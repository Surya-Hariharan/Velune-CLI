//go:build windows

package main

import (
	"os"
	"os/exec"
	"syscall"
)

// processAlive checks whether a process is still running on Windows.
// os.FindProcess always succeeds on Windows (returns a handle), so we
// open the process with PROCESS_QUERY_LIMITED_INFORMATION and read its
// exit code: STILL_ACTIVE (259) means the process has not yet exited.
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	const processQueryLimitedInformation = 0x1000
	const stillActive = 259
	h, err := syscall.OpenProcess(processQueryLimitedInformation, false, uint32(pid))
	if err != nil {
		return false
	}
	defer syscall.CloseHandle(h)
	var exitCode uint32
	if err := syscall.GetExitCodeProcess(h, &exitCode); err != nil {
		return false
	}
	return exitCode == stillActive
}

// setProcAttr creates the child in a new process group and detaches it
// from the console so it continues running after the launcher exits.
func setProcAttr(cmd *exec.Cmd) {
	const detachedProcess = 0x00000008
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP | detachedProcess,
	}
}

// gracefulStop on Windows has no SIGTERM equivalent for arbitrary processes;
// the Python daemon's signal handler catches KeyboardInterrupt via Ctrl+C.
// We send an interrupt event to the process group, then fall through to Kill.
func gracefulStop(proc *os.Process) error {
	return proc.Signal(os.Interrupt)
}
