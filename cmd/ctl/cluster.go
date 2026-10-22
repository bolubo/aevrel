package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"

	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/yaml"
)

const defaultClusterSecretSuffix = "-gateway-auth"

type clusterAddOptions struct {
	name     string
	endpoint string
	role     string
	secret   string
	token    string
	file     string
}

func executeClusterAdd(
	jsonOut bool,
	namespace string,
	args []string,
	out io.Writer,
) error {
	options, err := parseClusterAdd(args)
	if err != nil {
		return err
	}
	token := options.token
	if token == "" {
		token, err = randomToken()
		if err != nil {
			return fmt.Errorf("generate cluster gateway token: %w", err)
		}
	}
	secret := map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata": map[string]any{
			"name":      options.secret,
			"namespace": namespace,
		},
		"stringData": map[string]string{
			"token": token,
		},
	}
	cluster := map[string]any{
		"apiVersion": "shardferry.io/v1",
		"kind":       "ShardferryCluster",
		"metadata": map[string]any{
			"name":      options.name,
			"namespace": namespace,
		},
		"spec": map[string]any{
			"role":     options.role,
			"endpoint": options.endpoint,
			"secretRef": map[string]string{
				"name": options.secret,
				"key":  "token",
			},
		},
	}
	data, err := clusterAddManifest(secret, cluster, jsonOut)
	if err != nil {
		return err
	}
	if options.file == "" {
		_, err = out.Write(data)
		return err
	}
	file, err := os.OpenFile(
		options.file,
		os.O_WRONLY|os.O_CREATE|os.O_EXCL,
		0o600,
	)
	if err != nil {
		return fmt.Errorf("create %s without overwriting: %w", options.file, err)
	}
	complete := false
	defer func() {
		if !complete {
			_ = os.Remove(options.file)
		}
	}()
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return fmt.Errorf("write %s: %w", options.file, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close %s: %w", options.file, err)
	}
	complete = true
	fmt.Fprintf(out, "wrote cluster onboarding manifest to %s\n", options.file)
	fmt.Fprintf(out, "next: kubectl apply -f %s -n %s\n", options.file, namespace)
	return nil
}

func parseClusterAdd(args []string) (clusterAddOptions, error) {
	options := clusterAddOptions{role: "both"}
	if len(args) == 0 {
		return options, fmt.Errorf(
			"usage: shardferryctl cluster add NAME --endpoint URL [--role source|target|both] [--secret NAME] [--file PATH] [--token TOKEN]",
		)
	}
	if args[0] == "" || args[0][0] == '-' {
		return options, fmt.Errorf(
			"usage: shardferryctl cluster add NAME --endpoint URL",
		)
	}
	options.name = args[0]
	for index := 1; index < len(args); index++ {
		switch args[index] {
		case "--endpoint":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--endpoint requires a URL")
			}
			options.endpoint = args[index]
		case "--role":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--role requires source, target, or both")
			}
			options.role = args[index]
		case "--secret":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--secret requires a name")
			}
			options.secret = args[index]
		case "--token":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--token requires a value")
			}
			options.token = args[index]
		case "-f", "--file":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("%s requires a path", args[index-1])
			}
			options.file = args[index]
		case "-h", "--help":
			return options, fmt.Errorf(
				"usage: shardferryctl cluster add NAME --endpoint URL [--role source|target|both] [--secret NAME] [--file PATH] [--token TOKEN]",
			)
		default:
			return options, fmt.Errorf("unknown cluster add option: %s", args[index])
		}
	}
	if options.endpoint == "" {
		return options, fmt.Errorf("--endpoint is required")
	}
	if options.secret == "" {
		options.secret = options.name + defaultClusterSecretSuffix
	}
	if err := validateClusterAdd(options); err != nil {
		return options, err
	}
	return options, nil
}

func validateClusterAdd(options clusterAddOptions) error {
	if errors := validation.IsDNS1123Subdomain(options.name); len(errors) > 0 {
		return fmt.Errorf("invalid cluster name %q: %s", options.name, errors[0])
	}
	if options.secret != "" {
		if errors := validation.IsDNS1123Subdomain(options.secret); len(errors) > 0 {
			return fmt.Errorf("invalid secret name %q: %s", options.secret, errors[0])
		}
	}
	switch options.role {
	case "source", "target", "both":
	default:
		return fmt.Errorf("--role must be source, target, or both")
	}
	parsed, err := url.Parse(options.endpoint)
	if err != nil ||
		(parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.Host == "" ||
		parsed.User != nil ||
		parsed.RawQuery != "" ||
		parsed.Fragment != "" {
		return fmt.Errorf(
			"--endpoint must be an http:// or https:// URL without credentials, query, or fragment",
		)
	}
	return nil
}

func randomToken() (string, error) {
	data := make([]byte, 32)
	if _, err := rand.Read(data); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(data), nil
}

func clusterAddManifest(
	secret map[string]any,
	cluster map[string]any,
	jsonOut bool,
) ([]byte, error) {
	if jsonOut {
		data, err := json.MarshalIndent(
			map[string]any{
				"apiVersion": "v1",
				"kind":       "List",
				"items":      []any{secret, cluster},
			},
			"",
			"  ",
		)
		if err != nil {
			return nil, err
		}
		return append(data, '\n'), nil
	}
	secretData, err := yaml.Marshal(secret)
	if err != nil {
		return nil, err
	}
	clusterData, err := yaml.Marshal(cluster)
	if err != nil {
		return nil, err
	}
	data := make([]byte, 0, len(secretData)+len(clusterData)+5)
	data = append(data, secretData...)
	data = append(data, "---\n"...)
	data = append(data, clusterData...)
	return data, nil
}
