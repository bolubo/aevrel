//! Element-wise tensor reductions (sum / avg / max / min / copy).
//!
//! These kernels replace a numpy-based implementation, so they reproduce
//! numpy's accumulation semantics exactly rather than "textbook" float math:
//!
//! * `sum` accumulates **sequentially in the element dtype**, with two's
//!   complement wrap-around for integers and per-step rounding for float16
//!   (numpy's HALF loops round through float32 after every partial sum). The
//!   exception is float16 with a single output element, where numpy takes its
//!   pairwise float32 fast path, documented on `reduce_f16` below.
//! * `avg` accumulates in numpy's mean accumulator (float64 for integers,
//!   float32 for float16, the element dtype otherwise), divides by the input
//!   count in that accumulator, then narrows to the element dtype —
//!   truncating toward zero for integers and round-half-to-even for float16.
//! * `max` / `min` propagate NaN like `np.maximum` / `np.minimum`.
//! * `copy` is a pass-through and requires exactly one input.

use half::f16;
use std::cmp::Ordering;

/// Element dtype understood by the reduction kernel.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum DType {
    F64,
    F32,
    F16,
    I64,
    I32,
    I16,
    I8,
    U64,
    U32,
    U16,
    U8,
}

impl DType {
    /// Parse a numpy-compatible dtype name.
    ///
    /// `bfloat16` is deliberately absent: the retired implementation rejected
    /// it, and the Rust kernel has no bfloat16 element type either.
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "float64" | "f8" | "double" => Some(Self::F64),
            "float32" | "f4" | "float" => Some(Self::F32),
            "float16" | "f2" | "half" => Some(Self::F16),
            "int64" | "i8" | "longlong" => Some(Self::I64),
            "int32" | "i4" | "int" => Some(Self::I32),
            "int16" | "i2" | "short" => Some(Self::I16),
            "int8" | "i1" | "char" => Some(Self::I8),
            "uint64" | "u8" | "ulonglong" => Some(Self::U64),
            "uint32" | "u4" | "uint" => Some(Self::U32),
            "uint16" | "u2" | "ushort" => Some(Self::U16),
            "uint8" | "u1" | "ubyte" => Some(Self::U8),
            _ => None,
        }
    }

    pub fn item_size(self) -> usize {
        match self {
            Self::F64 | Self::I64 | Self::U64 => 8,
            Self::F32 | Self::I32 | Self::U32 => 4,
            Self::F16 | Self::I16 | Self::U16 => 2,
            Self::I8 | Self::U8 => 1,
        }
    }
}

/// Reduce a stack of equal-length buffers element-wise.
///
/// `inputs` holds one buffer per contributing source, in source order; the
/// accumulation order is exactly that order.
pub fn reduce_bytes(inputs: &[&[u8]], op: &str, dtype: &str) -> Result<Vec<u8>, String> {
    if inputs.is_empty() {
        return Err(format!("{op} requires at least one input"));
    }
    if op == "copy" {
        if inputs.len() != 1 {
            return Err("copy requires exactly one input".to_string());
        }
        return Ok(inputs[0].to_vec());
    }
    if dtype == "bfloat16" {
        return Err("bfloat16 reduction is not supported yet".to_string());
    }
    let element = DType::parse(dtype).ok_or_else(|| format!("unsupported dtype: {dtype}"))?;
    let item_size = element.item_size();
    let length = inputs[0].len();
    if !length.is_multiple_of(item_size) {
        return Err(format!(
            "reduction input length {length} is not a multiple of {item_size}"
        ));
    }
    if inputs.iter().any(|value| value.len() != length) {
        return Err("reduction inputs have different sizes".to_string());
    }
    match op {
        "sum" | "avg" | "max" | "min" => Ok(reduce(inputs, op, element)),
        other => Err(format!("unsupported reduction op: {other}")),
    }
}

fn reduce(inputs: &[&[u8]], op: &str, element: DType) -> Vec<u8> {
    let count = inputs.len();
    let mut output = vec![0u8; inputs[0].len()];
    match element {
        DType::F64 => reduce_float::<f64>(inputs, op, count, &mut output),
        DType::F32 => reduce_float::<f32>(inputs, op, count, &mut output),
        DType::F16 => reduce_f16(inputs, op, count, &mut output),
        DType::I64 => reduce_int::<i64>(inputs, op, count, &mut output),
        DType::I32 => reduce_int::<i32>(inputs, op, count, &mut output),
        DType::I16 => reduce_int::<i16>(inputs, op, count, &mut output),
        DType::I8 => reduce_int::<i8>(inputs, op, count, &mut output),
        DType::U64 => reduce_int::<u64>(inputs, op, count, &mut output),
        DType::U32 => reduce_int::<u32>(inputs, op, count, &mut output),
        DType::U16 => reduce_int::<u16>(inputs, op, count, &mut output),
        DType::U8 => reduce_int::<u8>(inputs, op, count, &mut output),
    }
    output
}

/// Read one native-endian element out of a buffer.
fn read<T: NativeElement>(buffer: &[u8], index: usize) -> T {
    let start = index * T::SIZE;
    T::from_ne_bytes(&buffer[start..start + T::SIZE])
}

/// Write one native-endian element into a buffer.
fn write<T: NativeElement>(buffer: &mut [u8], index: usize, value: T) {
    let start = index * T::SIZE;
    value.write_ne_bytes(&mut buffer[start..start + T::SIZE]);
}

/// Element type with a stable native-endian byte representation.
trait NativeElement: Copy {
    const SIZE: usize;
    fn from_ne_bytes(bytes: &[u8]) -> Self;
    fn write_ne_bytes(&self, out: &mut [u8]);
}

macro_rules! native_element {
    ($type:ty) => {
        impl NativeElement for $type {
            const SIZE: usize = std::mem::size_of::<$type>();
            fn from_ne_bytes(bytes: &[u8]) -> Self {
                let mut array = [0u8; std::mem::size_of::<$type>()];
                array.copy_from_slice(&bytes[..std::mem::size_of::<$type>()]);
                <$type>::from_ne_bytes(array)
            }
            fn write_ne_bytes(&self, out: &mut [u8]) {
                out[..std::mem::size_of::<$type>()].copy_from_slice(&self.to_ne_bytes());
            }
        }
    };
}

native_element!(f64);
native_element!(f32);
native_element!(i64);
native_element!(i32);
native_element!(i16);
native_element!(i8);
native_element!(u64);
native_element!(u32);
native_element!(u16);
native_element!(u8);

/// Integer reductions: wrapping sum, plain max/min, and a float64 mean that
/// truncates toward zero like `ndarray.astype`.
fn reduce_int<T>(inputs: &[&[u8]], op: &str, count: usize, output: &mut [u8])
where
    T: NativeElement + Ord + ToF64 + NarrowFromF64 + std::ops::Add<Output = T>,
{
    let elements = output.len() / T::SIZE;
    if op == "avg" {
        for index in 0..elements {
            let mut total = 0.0f64;
            for buffer in inputs {
                total += read::<T>(buffer, index).to_f64();
            }
            write::<T>(output, index, T::from_f64_truncating(total / count as f64));
        }
        return;
    }
    for index in 0..elements {
        let mut accumulator = read::<T>(inputs[0], index);
        for buffer in &inputs[1..] {
            let next = read::<T>(buffer, index);
            accumulator = match op {
                "sum" => accumulator + next,
                "max" => accumulator.max(next),
                _ => accumulator.min(next),
            };
        }
        write::<T>(output, index, accumulator);
    }
}

/// Lossy-but-lossless-enough integer to float64 widening for means.
trait ToF64 {
    fn to_f64(self) -> f64;
}

macro_rules! to_f64 {
    ($type:ty) => {
        impl ToF64 for $type {
            fn to_f64(self) -> f64 {
                self as f64
            }
        }
    };
}

to_f64!(i64);
to_f64!(i32);
to_f64!(i16);
to_f64!(i8);
to_f64!(u64);
to_f64!(u32);
to_f64!(u16);
to_f64!(u8);

/// Saturating float64 to integer narrowing (truncation toward zero).
trait NarrowFromF64 {
    fn from_f64_truncating(value: f64) -> Self;
}

macro_rules! narrow_from_f64 {
    ($type:ty) => {
        impl NarrowFromF64 for $type {
            fn from_f64_truncating(value: f64) -> Self {
                value as $type
            }
        }
    };
}

narrow_from_f64!(i64);
narrow_from_f64!(i32);
narrow_from_f64!(i16);
narrow_from_f64!(i8);
narrow_from_f64!(u64);
narrow_from_f64!(u32);
narrow_from_f64!(u16);
narrow_from_f64!(u8);

/// Float element with the arithmetic the reduction kernel needs.
trait FloatElement: NativeElement {
    fn from_count(count: usize) -> Self;
    fn nan_max(self, other: Self) -> Self;
    fn nan_min(self, other: Self) -> Self;
}

impl FloatElement for f64 {
    fn from_count(count: usize) -> Self {
        count as f64
    }
    fn nan_max(self, other: Self) -> Self {
        match self.partial_cmp(&other) {
            Some(Ordering::Less) => other,
            _ if self.is_nan() => self,
            _ if other.is_nan() => other,
            _ => self,
        }
    }
    fn nan_min(self, other: Self) -> Self {
        match self.partial_cmp(&other) {
            Some(Ordering::Greater) => other,
            _ if self.is_nan() => self,
            _ if other.is_nan() => other,
            _ => self,
        }
    }
}

impl FloatElement for f32 {
    fn from_count(count: usize) -> Self {
        count as f32
    }
    fn nan_max(self, other: Self) -> Self {
        f64::nan_max(self as f64, other as f64) as f32
    }
    fn nan_min(self, other: Self) -> Self {
        f64::nan_min(self as f64, other as f64) as f32
    }
}

/// Float32 / float64 reductions with NaN-propagating max/min.
fn reduce_float<T>(inputs: &[&[u8]], op: &str, count: usize, output: &mut [u8])
where
    T: FloatElement + std::ops::Add<Output = T> + std::ops::Div<Output = T>,
{
    let divisor = T::from_count(count);
    let elements = output.len() / T::SIZE;
    for index in 0..elements {
        let mut accumulator = read::<T>(inputs[0], index);
        for buffer in &inputs[1..] {
            let next = read::<T>(buffer, index);
            accumulator = match op {
                "sum" | "avg" => accumulator + next,
                "max" => accumulator.nan_max(next),
                _ => accumulator.nan_min(next),
            };
        }
        if op == "avg" {
            accumulator = accumulator / divisor;
        }
        write::<T>(output, index, accumulator);
    }
}

/// Float16 reductions.
///
/// `sum` routes every element through float32, which is what numpy's HALF
/// loops do: they quiet signaling NaNs and round after each partial sum.
/// `avg` accumulates in float32, divides in float32 and rounds once at the
/// end, mirroring `np.mean`'s float16 path. `max` / `min` compare through
/// float32 but return the winning operand's original bits, so a NaN — even a
/// signaling one — propagates exactly like `np.maximum` / `np.minimum`.
///
/// `sum` is the one op whose accumulation *shape* changes its result. numpy
/// lowers a reduction with exactly one output element onto its
/// `IS_BINARY_REDUCE` fast path, which is what `np.add.reduce` hits for an
/// `(n, 1)` input: the first operand widens to float32 and the rest is
/// pairwise-summed (also in float32) before a single final rounding. Two
/// things then differ from the element-wise path taken for wider outputs —
/// rounding is deferred instead of per-step, and float32 addition keeps the
/// *first* operand when both operands are NaN. So with one output element the
/// earliest NaN wins, and with many the latest one wins.
fn reduce_f16(inputs: &[&[u8]], op: &str, count: usize, output: &mut [u8]) {
    let elements = output.len() / 2;
    if op == "sum" && elements == 1 {
        let total = add_f32_left(widen_f16(inputs[0], 0), pairwise_sum_f32(&inputs[1..]));
        write::<u16>(output, 0, f16::from_f32(total).to_bits());
        return;
    }
    for index in 0..elements {
        let first = f16::from_bits(read::<u16>(inputs[0], index));
        let mut half = if op == "sum" {
            f16::from_f32(first.to_f32())
        } else {
            first
        };
        let mut wide = first.to_f32();
        for buffer in &inputs[1..] {
            let next = f16::from_bits(read::<u16>(buffer, index));
            match op {
                // The arriving element is the *left* operand: numpy's HALF
                // loop keeps the running sum in the second source register, so
                // a NaN here overrides one already accumulated.
                "sum" => half = f16::from_f32(add_f32_left(next.to_f32(), half.to_f32())),
                "avg" => wide += next.to_f32(),
                "max" => half = nan_max_f16(half, next),
                _ => half = nan_min_f16(half, next),
            }
        }
        let value = match op {
            "avg" => f16::from_f32(wide / count as f32),
            "sum" | "max" | "min" => half,
            _ => unreachable!("op is validated by the caller"),
        };
        write::<u16>(output, index, value.to_bits());
    }
}

/// numpy's `PW_BLOCKSIZE` from `loops_utils.h`.
const PW_BLOCKSIZE: usize = 128;

fn widen_f16(buffer: &[u8], index: usize) -> f32 {
    f16::from_bits(read::<u16>(buffer, index)).to_f32()
}

/// `left + right` in float32, keeping `left` when both operands are NaN.
///
/// IEEE-754 leaves the NaN choice to the implementation; x86 `addss` returns
/// the first source operand, and the golden fixtures encode what numpy
/// produced on that target, so the preference is spelled out here instead of
/// being inherited from whatever the codegen happens to emit.
fn add_f32_left(left: f32, right: f32) -> f32 {
    if left.is_nan() {
        left
    } else {
        left + right
    }
}

/// numpy's `HALF_pairwise_sum`, in float32 over float16 operands.
///
/// Below eight operands it sums sequentially from `-0.0`; up to `PW_BLOCKSIZE`
/// it uses eight accumulators plus a fixed reduction tree and then peels the
/// remaining tail; above that it halves at a multiple of eight and recurses.
/// Every addition keeps its left operand when both sides are NaN, which is what
/// the float32 loop numpy compiles this into does.
fn pairwise_sum_f32(values: &[&[u8]]) -> f32 {
    let n = values.len();
    if n < 8 {
        return values.iter().fold(-0.0f32, |sum, buffer| {
            add_f32_left(sum, widen_f16(buffer, 0))
        });
    }
    if n <= PW_BLOCKSIZE {
        let mut accumulators = [0f32; 8];
        for (slot, buffer) in accumulators.iter_mut().zip(&values[..8]) {
            *slot = widen_f16(buffer, 0);
        }
        let mut index = 8;
        while index <= n - 8 {
            for (slot, buffer) in accumulators.iter_mut().zip(&values[index..index + 8]) {
                *slot = add_f32_left(*slot, widen_f16(buffer, 0));
            }
            index += 8;
        }
        let low = add_f32_left(
            add_f32_left(accumulators[0], accumulators[1]),
            add_f32_left(accumulators[2], accumulators[3]),
        );
        let high = add_f32_left(
            add_f32_left(accumulators[4], accumulators[5]),
            add_f32_left(accumulators[6], accumulators[7]),
        );
        let mut total = add_f32_left(low, high);
        while index < n {
            total = add_f32_left(total, widen_f16(values[index], 0));
            index += 1;
        }
        return total;
    }
    // Halve at a multiple of the unrolled width so both halves stay aligned.
    let mut n2 = n / 2;
    n2 -= n2 % 8;
    add_f32_left(
        pairwise_sum_f32(&values[..n2]),
        pairwise_sum_f32(&values[n2..]),
    )
}

fn nan_max_f16(left: f16, right: f16) -> f16 {
    match left.to_f32().partial_cmp(&right.to_f32()) {
        Some(Ordering::Less) => right,
        _ if left.is_nan() => left,
        _ if right.is_nan() => right,
        _ => left,
    }
}

fn nan_min_f16(left: f16, right: f16) -> f16 {
    match left.to_f32().partial_cmp(&right.to_f32()) {
        Some(Ordering::Greater) => right,
        _ if left.is_nan() => left,
        _ if right.is_nan() => right,
        _ => left,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f32_bytes(values: &[f32]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_ne_bytes())
            .collect()
    }

    fn as_f32(bytes: &[u8]) -> Vec<f32> {
        bytes
            .as_chunks::<4>()
            .0
            .iter()
            .map(|chunk| f32::from_ne_bytes(*chunk))
            .collect()
    }

    fn f16_bytes(values: &[f16]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_bits().to_ne_bytes())
            .collect()
    }

    fn as_f16(bytes: &[u8]) -> Vec<f16> {
        bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|chunk| f16::from_bits(u16::from_ne_bytes(*chunk)))
            .collect()
    }

    #[test]
    fn copy_requires_exactly_one_input() {
        let data = f32_bytes(&[1.0, 2.0]);
        assert_eq!(reduce_bytes(&[&data], "copy", "float32").unwrap(), data);
        assert_eq!(
            reduce_bytes(&[&data, &data], "copy", "float32").unwrap_err(),
            "copy requires exactly one input"
        );
    }

    #[test]
    fn sum_wraps_like_numpy() {
        let left = (-1i64).to_ne_bytes().to_vec();
        let right = 2i64.to_ne_bytes().to_vec();
        let summed = reduce_bytes(&[&left, &right], "sum", "int64").unwrap();
        assert_eq!(i64::from_ne_bytes(summed[..8].try_into().unwrap()), 1);
    }

    #[test]
    fn avg_truncates_toward_zero() {
        // (-3 + 2) / 2 == -0.5, which truncates to 0 exactly like astype.
        let left = (-3i32).to_ne_bytes().to_vec();
        let right = 2i32.to_ne_bytes().to_vec();
        let averaged = reduce_bytes(&[&left, &right], "avg", "int32").unwrap();
        assert_eq!(i32::from_ne_bytes(averaged[..4].try_into().unwrap()), 0);
    }

    #[test]
    fn float16_sum_rounds_after_every_step() {
        // 2048 + 3 is 2051, which sits exactly between 2050 and 2052 in float16
        // and therefore rounds to the even neighbour 2052.
        let left = f16_bytes(&[f16::from_f32(2048.0), f16::from_f32(2048.0)]);
        let right = f16_bytes(&[f16::from_f32(3.0), f16::from_f32(3.0)]);
        let summed = as_f16(&reduce_bytes(&[&left, &right], "sum", "float16").unwrap());
        assert_eq!(summed[0].to_f32(), 2052.0);
        // float32 accumulation keeps 2051/2 = 1025.5, which rounds to 1026.
        let averaged = as_f16(&reduce_bytes(&[&left, &right], "avg", "float16").unwrap());
        assert_eq!(averaged[0].to_f32(), 1026.0);
    }

    #[test]
    fn nan_propagates_through_max_min_and_sum() {
        let left = f32_bytes(&[f32::NAN, 1.0]);
        let right = f32_bytes(&[0.0, 1.0]);
        for op in ["max", "min", "sum", "avg"] {
            let values = as_f32(&reduce_bytes(&[&left, &right], op, "float32").unwrap());
            assert!(values[0].is_nan(), "{op} must propagate NaN");
            assert_eq!(values[1], if op == "sum" { 2.0 } else { 1.0 });
        }
        // NaN in the *second* operand must propagate too.
        for op in ["max", "min"] {
            let values = as_f32(&reduce_bytes(&[&right, &left], op, "float32").unwrap());
            assert!(
                values[0].is_nan(),
                "{op} must propagate NaN from either side"
            );
        }
        let values = as_f16(
            &reduce_bytes(
                &[
                    &f16_bytes(&[f16::NAN, f16::from_f32(1.0)]),
                    &f16_bytes(&[f16::from_f32(0.0), f16::from_f32(1.0)]),
                ],
                "max",
                "float16",
            )
            .unwrap(),
        );
        assert!(values[0].is_nan());
        assert_eq!(values[1].to_f32(), 1.0);
    }

    #[test]
    fn unsupported_dtype_and_op_are_rejected() {
        let data = f32_bytes(&[1.0]);
        assert_eq!(
            reduce_bytes(&[&data], "sum", "bfloat16").unwrap_err(),
            "bfloat16 reduction is not supported yet"
        );
        assert_eq!(
            reduce_bytes(&[&data], "sum", "float128").unwrap_err(),
            "unsupported dtype: float128"
        );
        assert_eq!(
            reduce_bytes(&[&data], "prod", "float32").unwrap_err(),
            "unsupported reduction op: prod"
        );
        assert_eq!(
            reduce_bytes(&[], "sum", "float32").unwrap_err(),
            "sum requires at least one input"
        );
        assert_eq!(
            reduce_bytes(&[&data, &f32_bytes(&[1.0, 2.0])], "sum", "float32").unwrap_err(),
            "reduction inputs have different sizes"
        );
    }
}
