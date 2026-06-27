//go:build !windows

package main

import (
	"os"
	"os/exec"
	"syscall"
)

// processAlive returns true when a process with the given PID is running.
// On Unix, Signal(0) is a no-op that errors only when the process is gone
// or we lack permission to signal it.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return proc.Signal(syscall.Signal(0)) == nil
}

// setProcAttr detaches the child from the launcher's controlling terminal
// so the daemon survives after the launcher exits.
func setProcAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}

// gracefulStop sends SIGTERM to ask the daemon to shut down cleanly.
func gracefulStop(proc *os.Process) error {
	return proc.Signal(syscall.SIGTERM)
}
