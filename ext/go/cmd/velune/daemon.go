package main

// Daemon lifecycle management.
//
// Go owns the process lifecycle: starting, stopping, and health-checking the
// Python daemon process.  Python owns everything inside that process: the IPC
// protocol, AI orchestration, and business logic.
//
// The daemon is identified by a PID file at ~/.velune/daemon.pid.  Go never
// reads or speaks the IPC wire format — that belongs to Python.

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// runDaemonCommand dispatches daemon sub-commands.
// velune daemon start [workspace]
// velune daemon stop
// velune daemon status
func runDaemonCommand(action string, rest []string) {
	switch action {
	case "start":
		workspace := "."
		if len(rest) > 0 {
			workspace = rest[0]
		}
		if err := daemonStart(workspace); err != nil {
			fmt.Fprintf(os.Stderr, "velune daemon: start failed: %v\n", err)
			os.Exit(1)
		}

	case "stop":
		if err := daemonStop(); err != nil {
			fmt.Fprintf(os.Stderr, "velune daemon: stop failed: %v\n", err)
			os.Exit(1)
		}

	case "status":
		daemonStatus()

	default:
		fmt.Fprintf(os.Stderr, "velune daemon: unknown action %q\n", action)
		fmt.Fprintln(os.Stderr, "usage: velune daemon <start|stop|status> [workspace]")
		os.Exit(1)
	}
}

// ─── Start ────────────────────────────────────────────────────────────────────

func daemonStart(workspace string) error {
	pidFile := daemonPidFile()

	// If already running, report and exit cleanly.
	if pid, err := readPid(pidFile); err == nil {
		if processAlive(pid) {
			fmt.Printf("velune daemon: already running (pid %d)\n", pid)
			return nil
		}
		// Stale PID — clean it up before starting fresh.
		_ = os.Remove(pidFile)
	}

	python := discoverPython()

	// Resolve workspace to an absolute path so the daemon process always has a
	// stable working path regardless of where the launcher is invoked from.
	absWorkspace, err := filepath.Abs(workspace)
	if err != nil {
		return fmt.Errorf("resolve workspace: %w", err)
	}
	if _, err := os.Stat(absWorkspace); err != nil {
		return fmt.Errorf("workspace %q does not exist", absWorkspace)
	}

	cmd := exec.Command(python, "-m", "velune.daemon.server", absWorkspace)
	cmd.Env = os.Environ()

	// Detach from the terminal so the daemon survives after the launcher exits.
	setProcAttr(cmd)

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("spawn: %w", err)
	}

	pid := cmd.Process.Pid
	fmt.Printf("velune daemon: started (pid %d, workspace %s)\n", pid, absWorkspace)

	// The Python daemon writes its own PID file once it is ready.  Poll for it
	// so we can confirm a successful start before returning.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := readPid(pidFile); err == nil {
			fmt.Println("velune daemon: ready")
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}

	// Daemon did not write PID file in time — may still be starting up.
	fmt.Fprintf(os.Stderr,
		"velune daemon: warning: PID file not seen after 5s (pid %d may still be starting)\n", pid)
	return nil
}

// ─── Stop ─────────────────────────────────────────────────────────────────────

func daemonStop() error {
	pidFile := daemonPidFile()
	pid, err := readPid(pidFile)
	if err != nil {
		fmt.Println("velune daemon: not running")
		return nil
	}

	proc, err := os.FindProcess(pid)
	if err != nil || !processAlive(pid) {
		fmt.Println("velune daemon: not running (stale PID file removed)")
		_ = os.Remove(pidFile)
		return nil
	}

	// Graceful shutdown first.
	if err := gracefulStop(proc); err != nil {
		return fmt.Errorf("signal: %w", err)
	}

	// Wait up to 5 s for clean exit.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if !processAlive(pid) {
			_ = os.Remove(pidFile)
			fmt.Println("velune daemon: stopped")
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}

	// Force-kill if still alive.
	_ = proc.Kill()
	_ = os.Remove(pidFile)
	fmt.Println("velune daemon: killed (did not exit cleanly)")
	return nil
}

// ─── Status ───────────────────────────────────────────────────────────────────

func daemonStatus() {
	pidFile := daemonPidFile()
	pid, err := readPid(pidFile)
	if err != nil {
		fmt.Println("velune daemon: stopped")
		return
	}

	if processAlive(pid) {
		fmt.Printf("velune daemon: running (pid %d)\n", pid)
	} else {
		fmt.Println("velune daemon: stopped (stale PID file)")
		_ = os.Remove(pidFile)
	}
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

func daemonPidFile() string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".velune", "daemon.pid")
}

func readPid(pidFile string) (int, error) {
	data, err := os.ReadFile(pidFile)
	if err != nil {
		return 0, err
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		return 0, errors.New("malformed PID file")
	}
	return pid, nil
}
