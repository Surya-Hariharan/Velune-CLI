package main

// Update management.
//
// Go owns the update command: it knows where Python is, what pip is, and
// how to invoke the upgrade. Python never needs to know how it was installed
// or upgraded — that is a launcher responsibility.
//
// velune update [--check]
//   --check   Print whether an upgrade is available but do not install it.
//             Exit 0 if up-to-date, exit 2 if an upgrade is available.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"
)

const pypiURL = "https://pypi.org/pypi/velune/json"

type pypiRelease struct {
	Info struct {
		Version string `json:"version"`
	} `json:"info"`
}

// runUpdate handles `velune update [--check]`.
func runUpdate(args []string) {
	checkOnly := false
	for _, a := range args {
		if a == "--check" {
			checkOnly = true
		}
	}

	current := readPythonVersion()
	fmt.Printf("current version: %s\n", current)

	latest, err := fetchLatestVersion()
	if err != nil {
		// PyPI is unreachable — don't fail hard, just warn.
		fmt.Fprintf(os.Stderr, "velune update: could not reach PyPI: %v\n", err)
		fmt.Fprintf(os.Stderr, "  Run manually: pip install --upgrade velune\n")
		os.Exit(1)
	}

	fmt.Printf("latest version:  %s\n", latest)

	if versionEqual(current, latest) {
		fmt.Println("✓ Already up to date.")
		return
	}

	fmt.Printf("→ Upgrade available: %s → %s\n", current, latest)

	if checkOnly {
		// Exit 2 signals "upgrade available" to scripts.
		os.Exit(2)
	}

	python := discoverPython()
	if err := upgradePip(python); err != nil {
		fmt.Fprintf(os.Stderr, "velune update: pip upgrade failed: %v\n", err)
		os.Exit(1)
	}

	newVer := readPythonVersion()
	fmt.Printf("✓ Updated to v%s\n", newVer)
}

// fetchLatestVersion queries PyPI for the latest published velune version.
func fetchLatestVersion() (string, error) {
	return fetchLatestVersionFromURL(pypiURL)
}

// fetchLatestVersionFromURL is the testable core — accepts any URL.
func fetchLatestVersionFromURL(url string) (string, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("PyPI returned HTTP %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return "", err
	}

	var release pypiRelease
	if err := json.Unmarshal(body, &release); err != nil {
		return "", fmt.Errorf("parse PyPI response: %w", err)
	}

	if release.Info.Version == "" {
		return "", fmt.Errorf("PyPI response contained no version")
	}
	return release.Info.Version, nil
}

// upgradePip runs `pip install --upgrade velune` using the discovered Python.
func upgradePip(python string) error {
	args := []string{"-m", "pip", "install", "--upgrade", "velune"}
	cmd := exec.Command(python, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()
	return cmd.Run()
}

// versionEqual returns true when both version strings are the same after
// normalising whitespace and stripping a leading "v".
func versionEqual(a, b string) bool {
	return normaliseVersion(a) == normaliseVersion(b)
}

func normaliseVersion(v string) string {
	v = strings.TrimSpace(v)
	v = strings.TrimPrefix(v, "v")
	return v
}
