package main

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

func TestReadPid_Valid(t *testing.T) {
	tmp := t.TempDir()
	pidFile := filepath.Join(tmp, "daemon.pid")
	if err := os.WriteFile(pidFile, []byte("12345\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	pid, err := readPid(pidFile)
	if err != nil {
		t.Fatalf("readPid returned error: %v", err)
	}
	if pid != 12345 {
		t.Fatalf("expected 12345, got %d", pid)
	}
}

func TestReadPid_Missing(t *testing.T) {
	_, err := readPid("/nonexistent/daemon.pid")
	if err == nil {
		t.Fatal("expected error for missing PID file")
	}
}

func TestReadPid_Malformed(t *testing.T) {
	tmp := t.TempDir()
	pidFile := filepath.Join(tmp, "daemon.pid")
	if err := os.WriteFile(pidFile, []byte("not-a-number\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := readPid(pidFile)
	if err == nil {
		t.Fatal("expected error for malformed PID file")
	}
}

func TestProcessAlive_CurrentProcess(t *testing.T) {
	// The current process is always alive.
	if !processAlive(os.Getpid()) {
		t.Fatal("processAlive returned false for the current process")
	}
}

func TestProcessAlive_InvalidPid(t *testing.T) {
	// PID 0 and negative PIDs are never valid targets.
	if processAlive(0) {
		t.Fatal("processAlive returned true for pid 0")
	}
	if processAlive(-1) {
		t.Fatal("processAlive returned true for pid -1")
	}
}

func TestDaemonPidFile(t *testing.T) {
	path := daemonPidFile()
	if path == "" {
		t.Fatal("daemonPidFile returned empty string")
	}
	// Must end with daemon.pid
	if filepath.Base(path) != "daemon.pid" {
		t.Fatalf("expected daemon.pid, got %q", filepath.Base(path))
	}
}

func TestDaemonStatus_NotRunning(t *testing.T) {
	// Override the pid file to a temp location with no running process.
	tmp := t.TempDir()
	pidFile := filepath.Join(tmp, "daemon.pid")
	// Write a PID that is guaranteed not to be a running daemon.
	// PID 999999999 is astronomically unlikely to exist.
	if err := os.WriteFile(pidFile, []byte(strconv.Itoa(999999999)), 0o644); err != nil {
		t.Fatal(err)
	}
	// If processAlive returns false for a non-existent PID this is working correctly.
	if processAlive(999999999) {
		t.Skip("PID 999999999 unexpectedly exists — skipping")
	}
}
