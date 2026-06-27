package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
)

const launcherVersion = "1"

func main() {
	args := os.Args[1:]

	if len(args) > 0 {
		switch args[0] {
		case "--version", "-V":
			jsonMode := false
			for _, a := range args {
				if a == "--json" {
					jsonMode = true
				}
			}
			ver := readPythonVersion()
			if jsonMode {
				fmt.Printf("{\"version\": \"%s\", \"launcher\": \"%s\"}\n", ver, launcherVersion)
			} else {
				fmt.Printf("velune v%s\n", ver)
			}
			return

		case "--health":
			runHealthCheck()
			return

		case "daemon":
			if len(args) < 2 {
				fmt.Fprintln(os.Stderr, "usage: velune daemon <start|stop|status> [workspace]")
				os.Exit(1)
			}
			runDaemonCommand(args[1], args[2:])
			return

		case "update":
			runUpdate(args[1:])
			return
		}
	}

	python := discoverPython()
	logLaunch(python, args)
	runPythonEngine(python, args)
}

// ─── Python discovery ────────────────────────────────────────────────────────

// discoverPython returns the best Python executable to use.
// Priority order:
//  1. .venv next to the launcher binary (installed package scenario)
//  2. VIRTUAL_ENV env var (caller activated a venv)
//  3. python3 / python in PATH
func discoverPython() string {
	// 1. Sibling .venv
	if exe, err := os.Executable(); err == nil {
		base := filepath.Dir(exe)
		for _, rel := range venvCandidates() {
			candidate := filepath.Join(base, rel)
			if isExecutable(candidate) {
				return candidate
			}
		}
		// Also check one directory up (binary is in bin/ or Scripts/)
		parent := filepath.Dir(base)
		for _, rel := range venvCandidates() {
			candidate := filepath.Join(parent, rel)
			if isExecutable(candidate) {
				return candidate
			}
		}
	}

	// 2. Caller's activated virtual environment
	if venv := os.Getenv("VIRTUAL_ENV"); venv != "" {
		for _, rel := range venvCandidates() {
			candidate := filepath.Join(venv, rel)
			if isExecutable(candidate) {
				return candidate
			}
		}
	}

	// 3. PATH lookup
	for _, name := range []string{"python3", "python"} {
		if path, err := exec.LookPath(name); err == nil {
			return path
		}
	}

	return "python"
}

// venvCandidates returns relative paths to the Python interpreter inside a venv.
func venvCandidates() []string {
	if runtime.GOOS == "windows" {
		return []string{
			filepath.Join(".venv", "Scripts", "python.exe"),
			filepath.Join("venv", "Scripts", "python.exe"),
		}
	}
	return []string{
		filepath.Join(".venv", "bin", "python3"),
		filepath.Join(".venv", "bin", "python"),
		filepath.Join("venv", "bin", "python3"),
		filepath.Join("venv", "bin", "python"),
	}
}

func isExecutable(path string) bool {
	info, err := os.Stat(path)
	if err != nil {
		return false
	}
	if info.IsDir() {
		return false
	}
	if runtime.GOOS == "windows" {
		return true // os.Stat success is enough on Windows
	}
	return info.Mode()&0o111 != 0
}

// ─── Engine runner ────────────────────────────────────────────────────────────

func runPythonEngine(python string, args []string) {
	cmdArgs := append([]string{"-m", "velune"}, args...)
	cmd := exec.Command(python, cmdArgs...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()

	// Start the child before setting up signal forwarding so we have a PID.
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "velune: failed to start Python engine: %v\n", err)
		os.Exit(1)
	}

	// Forward OS signals to the child process.
	stop := forwardSignals(cmd)
	defer stop()

	if err := cmd.Wait(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			os.Exit(exit.ExitCode())
		}
		fmt.Fprintf(os.Stderr, "velune: Python engine error: %v\n", err)
		os.Exit(1)
	}
}

// forwardSignals relays SIGINT/SIGTERM to the child process and returns a
// stop function that cleans up the goroutine.
func forwardSignals(cmd *exec.Cmd) func() {
	ch := make(chan os.Signal, 4)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)

	go func() {
		for sig := range ch {
			if cmd.Process != nil {
				_ = cmd.Process.Signal(sig)
			}
		}
	}()

	return func() {
		signal.Stop(ch)
		close(ch)
	}
}

// ─── Health check ─────────────────────────────────────────────────────────────

func runHealthCheck() {
	python := discoverPython()

	type check struct {
		Name   string `json:"name"`
		Status string `json:"status"`
		Detail string `json:"detail,omitempty"`
	}

	checks := []check{}

	// 1. Python binary
	if python == "python" {
		if _, err := exec.LookPath("python"); err != nil {
			checks = append(checks, check{"python_binary", "fail", "not found in PATH"})
		} else {
			checks = append(checks, check{"python_binary", "ok", python})
		}
	} else {
		checks = append(checks, check{"python_binary", "ok", python})
	}

	// 2. Python version
	out, err := exec.Command(python, "--version").Output()
	if err != nil {
		checks = append(checks, check{"python_version", "fail", err.Error()})
	} else {
		ver := strings.TrimSpace(string(out))
		checks = append(checks, check{"python_version", "ok", ver})
	}

	// 3. Velune module importable
	out, err = exec.Command(python, "-c", "import velune; print(velune.__version__)").Output()
	if err != nil {
		checks = append(checks, check{"velune_module", "fail", "cannot import velune"})
	} else {
		checks = append(checks, check{"velune_module", "ok", strings.TrimSpace(string(out))})
	}

	// 4. Launcher version
	checks = append(checks, check{"launcher_version", "ok", launcherVersion})

	// Print summary
	allOk := true
	for _, c := range checks {
		icon := "✓"
		if c.Status != "ok" {
			icon = "✗"
			allOk = false
		}
		if c.Detail != "" {
			fmt.Printf("  %s %-20s %s\n", icon, c.Name, c.Detail)
		} else {
			fmt.Printf("  %s %s\n", icon, c.Name)
		}
	}

	if !allOk {
		os.Exit(1)
	}
}

// ─── Version reading ──────────────────────────────────────────────────────────

func readPythonVersion() string {
	exe, err := os.Executable()
	if err != nil {
		return "unknown"
	}
	base := filepath.Dir(exe)

	locations := []string{
		filepath.Join(base, "velune", "__init__.py"),
		filepath.Join(base, "..", "velune", "__init__.py"),
		filepath.Join(base, "..", "..", "velune", "__init__.py"),
		"velune/__init__.py",
	}
	for _, loc := range locations {
		if v, err := extractVersion(loc); err == nil {
			return v
		}
	}
	return "unknown"
}

func extractVersion(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "__version__") {
			parts := strings.SplitN(line, "=", 2)
			if len(parts) == 2 {
				return strings.Trim(strings.TrimSpace(parts[1]), `"'`), nil
			}
		}
	}
	return "", fmt.Errorf("not found")
}

// ─── Structured logging ───────────────────────────────────────────────────────

type launchLog struct {
	Time    string   `json:"time"`
	Python  string   `json:"python"`
	Args    []string `json:"args"`
	OS      string   `json:"os"`
	Arch    string   `json:"arch"`
	Version string   `json:"version"`
}

func logLaunch(python string, args []string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return
	}
	logDir := filepath.Join(home, ".velune")
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return
	}
	logPath := filepath.Join(logDir, "launcher.log")

	f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()

	entry := launchLog{
		Time:    time.Now().UTC().Format(time.RFC3339),
		Python:  python,
		Args:    args,
		OS:      runtime.GOOS,
		Arch:    runtime.GOARCH,
		Version: readPythonVersion(),
	}
	data, err := json.Marshal(entry)
	if err != nil {
		return
	}
	_, _ = f.Write(append(data, '\n'))
}
