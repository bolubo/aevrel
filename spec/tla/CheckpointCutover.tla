---------------- MODULE CheckpointCutover ----------------
EXTENDS Naturals

CONSTANTS Versions, Owners, Epochs

VARIABLES
  current,
  phase,
  fenceEpoch,
  fenceOwner,
  fenceVersion,
  manifestReady,
  paused

vars == <<current, phase, fenceEpoch, fenceOwner, fenceVersion,
          manifestReady, paused>>

Init ==
  /\ current = "NONE"
  /\ phase = "following"
  /\ fenceEpoch = 0
  /\ fenceOwner = "NONE"
  /\ fenceVersion = "NONE"
  /\ manifestReady = FALSE
  /\ paused = FALSE

Acquire(owner, epoch, version) ==
  /\ owner \in Owners
  /\ epoch \in Epochs
  /\ epoch > 0
  /\ version \in Versions
  /\ phase = "following"
  /\ epoch >= fenceEpoch
  /\ (epoch = fenceEpoch => owner = fenceOwner)
  /\ (epoch > fenceEpoch \/ fenceOwner = "NONE")
  /\ fenceEpoch' = epoch
  /\ fenceOwner' = owner
  /\ fenceVersion' = version
  /\ UNCHANGED <<current, phase, manifestReady, paused>>

FollowNext ==
  /\ phase = "promoted"
  /\ phase' = "following"
  /\ UNCHANGED <<current, fenceEpoch, fenceOwner, fenceVersion,
                 manifestReady, paused>>

ManifestReady ==
  /\ phase = "following"
  /\ ~manifestReady
  /\ manifestReady' = TRUE
  /\ UNCHANGED <<current, phase, fenceEpoch, fenceOwner, fenceVersion,
                 paused>>

Pause ==
  /\ phase = "following"
  /\ manifestReady
  /\ phase' = "paused"
  /\ paused' = TRUE
  /\ UNCHANGED <<current, fenceEpoch, fenceOwner, fenceVersion,
                 manifestReady>>

FinalDelta(v) ==
  /\ phase = "paused"
  /\ v = fenceVersion
  /\ v \in Versions
  /\ manifestReady
  /\ UNCHANGED vars

Promote(v) ==
  /\ phase = "paused"
  /\ manifestReady
  /\ v = fenceVersion
  /\ current' = v
  /\ phase' = "promoted"
  /\ paused' = FALSE
  /\ UNCHANGED <<fenceEpoch, fenceOwner, fenceVersion, manifestReady>>

Resume ==
  /\ phase = "paused"
  /\ phase' = "following"
  /\ paused' = FALSE
  /\ UNCHANGED <<current, fenceEpoch, fenceOwner, fenceVersion,
                 manifestReady>>

Crash ==
  /\ UNCHANGED vars

Next ==
  \/ \E owner \in Owners, epoch \in Epochs, version \in Versions:
       Acquire(owner, epoch, version)
  \/ FollowNext
  \/ ManifestReady
  \/ Pause
  \/ \E version \in Versions: FinalDelta(version)
  \/ \E version \in Versions: Promote(version)
  \/ Resume
  \/ Crash

Spec == Init /\ [][Next]_vars

FenceMonotonic ==
  phase = "promoted" => fenceVersion # "NONE"

NoMixedCurrent ==
  current \in Versions \cup {"NONE"}

PromotionIsFenced ==
  phase = "promoted" => current = fenceVersion

PausedRequiresManifest ==
  phase = "paused" => manifestReady

Invariant ==
  /\ FenceMonotonic
  /\ NoMixedCurrent
  /\ PromotionIsFenced
  /\ PausedRequiresManifest

THEOREM Spec => []Invariant
=============================================================
