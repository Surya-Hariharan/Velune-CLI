package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExtractVersion(t *testing.T) {
	tmp := t.TempDir()
	initPy := filepath.Join(tmp, "__init__.py")
	if err := os.WriteFile(initPy, []byte("__version__ = \"0.9.5\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := extractVersion(initPy)
	if err != nil {
		t.Fatalf("extractVersion returned error: %v", err)
	}
	if got != "0.9.5" {
		t.Fatalf("expected 0.9.5, got %q", got)
	}
}

func TestExtractVersionSingleQuotes(t *testing.T) {
	tmp := t.TempDir()
	initPy := filepath.Join(tmp, "__init__.py")
	if err := os.WriteFile(initPy, []byte("__version__ = '1.2.3'\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := extractVersion(initPy)
	if err != nil {
		t.Fatal(err)
	}
	if got != "1.2.3" {
		t.Fatalf("expected 1.2.3, got %q", got)
	}
}

func TestExtractVersionMissing(t *testing.T) {
	tmp := t.TempDir()
	initPy := filepath.Join(tmp, "__init__.py")
	if err := os.WriteFile(initPy, []byte("# no version here\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := extractVersion(initPy)
	if err == nil {
		t.Fatal("expected error for missing __version__, got nil")
	}
}

func TestVenvCandidates(t *testing.T) {
	candidates := venvCandidates()
	if len(candidates) == 0 {
		t.Fatal("venvCandidates returned empty list")
	}
	for _, c := range candidates {
		if strings.Contains(c, "python") || strings.Contains(c, "Python") {
			return
		}
	}
	t.Fatal("none of the candidates contain 'python'")
}

func TestIsExecutable_MissingFile(t *testing.T) {
	if isExecutable("/nonexistent/path/python") {
		t.Fatal("isExecutable returned true for non-existent path")
	}
}

func TestLogLaunch_DoesNotPanic(t *testing.T) {
	// Should complete without panicking, even if home dir is unusual.
	logLaunch("python", []string{"--help"})
}
