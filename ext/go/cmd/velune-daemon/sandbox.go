package main

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"time"

	"github.com/shirou/gopsutil/v3/process"
)

type SandboxResult struct {
	ExitCode     int     `json:"exit_code"`
	Stdout       string  `json:"stdout"`
	Stderr       string  `json:"stderr"`
	DurationMs   float64 `json:"duration_ms"`
	PeakMemoryMb float64 `json:"peak_memory_mb"`
}

type Sandbox struct {
	MaxOutputBytes int
	MaxMemoryMb    float64
}

// BoundedBuffer limits the amount of data read to prevent memory exhaustion
type BoundedBuffer struct {
	limit     int
	buf       bytes.Buffer
	truncated bool
}

func (b *BoundedBuffer) Write(p []byte) (n int, err error) {
	if b.buf.Len() >= b.limit {
		b.truncated = true
		return len(p), nil // Discard but pretend we wrote it
	}
	allowed := b.limit - b.buf.Len()
	if len(p) > allowed {
		b.buf.Write(p[:allowed])
		b.truncated = true
		return len(p), nil // Discard remainder
	}
	return b.buf.Write(p)
}

func (b *BoundedBuffer) String() string {
	if b.truncated {
		return b.buf.String() + "\n[velune: output truncated]"
	}
	return b.buf.String()
}

func (s *Sandbox) Execute(ctx context.Context, cmdName string, args []string, dir string, timeout time.Duration) (*SandboxResult, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, cmdName, args...)
	cmd.Dir = dir

	stdoutBuf := &BoundedBuffer{limit: s.MaxOutputBytes}
	stderrBuf := &BoundedBuffer{limit: s.MaxOutputBytes}

	cmd.Stdout = stdoutBuf
	cmd.Stderr = stderrBuf

	start := time.Now()
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("failed to start process: %w", err)
	}

	// Memory monitoring goroutine
	pid := int32(cmd.Process.Pid)
	peakMem := 0.0
	done := make(chan struct{})

	go func() {
		proc, err := process.NewProcess(pid)
		if err != nil {
			close(done)
			return
		}

		ticker := time.NewTicker(100 * time.Millisecond)
		defer ticker.Stop()

		for {
			select {
			case <-done:
				return
			case <-ticker.C:
				memInfo, err := proc.MemoryInfo()
				if err == nil {
					memMb := float64(memInfo.RSS) / (1024 * 1024)
					if memMb > peakMem {
						peakMem = memMb
					}
					if s.MaxMemoryMb > 0 && memMb > s.MaxMemoryMb {
						// Kill process if over memory limit
						_ = cmd.Process.Kill()
						return
					}
				}
			}
		}
	}()

	err := cmd.Wait()
	close(done)

	duration := time.Since(start).Seconds() * 1000

	exitCode := 0
	if err != nil {
		if exitError, ok := err.(*exec.ExitError); ok {
			exitCode = exitError.ExitCode()
		} else {
			exitCode = -1
		}
	}

	return &SandboxResult{
		ExitCode:     exitCode,
		Stdout:       stdoutBuf.String(),
		Stderr:       stderrBuf.String(),
		DurationMs:   duration,
		PeakMemoryMb: peakMem,
	}, nil
}
