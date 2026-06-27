package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func main() {
	args := os.Args[1:]

	// Fast-path: --version
	if len(args) > 0 && (args[0] == "--version" || args[0] == "-V") {
		// Handle optional --json flag
		jsonMode := false
		for _, arg := range args {
			if arg == "--json" {
				jsonMode = true
				break
			}
		}

		version := getVersion()
		if jsonMode {
			fmt.Printf("{\"version\": \"%s\"}\n", version)
		} else {
			fmt.Printf("velune v%s\n", version)
		}
		os.Exit(0)
	}

	// For all other commands, pass-through to Python engine
	// This delegates complex argument parsing and execution to the existing Typer CLI
	runPythonEngine(args)
}

// getVersion attempts to read the version directly from velune/__init__.py
func getVersion() string {
	// Find the absolute path to the current executable
	exePath, err := os.Executable()
	if err != nil {
		return "unknown"
	}

	exeDir := filepath.Dir(exePath)

	// Assuming the binary is built and placed in the project root,
	// or we are running `go run` in the project root.
	// We check a few common locations for velune/__init__.py
	locations := []string{
		filepath.Join(exeDir, "velune", "__init__.py"),
		filepath.Join(exeDir, "..", "velune", "__init__.py"),
		filepath.Join(exeDir, "..", "..", "velune", "__init__.py"),
		"velune/__init__.py", // Current working directory fallback
	}

	for _, loc := range locations {
		if content, err := extractVersionFromFile(loc); err == nil {
			return content
		}
	}

	return "unknown"
}

func extractVersionFromFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "__version__") {
			parts := strings.Split(line, "=")
			if len(parts) >= 2 {
				version := strings.TrimSpace(parts[1])
				version = strings.Trim(version, "\"'")
				return version, nil
			}
		}
	}
	return "", fmt.Errorf("version not found in file")
}

func runPythonEngine(args []string) {
	// Discover python executable
	pythonExec := getPythonExec()

	// Prepare arguments: `-m velune` followed by all passed arguments
	cmdArgs := append([]string{"-m", "velune"}, args...)

	cmd := exec.Command(pythonExec, cmdArgs...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	// Pass through the environment
	cmd.Env = os.Environ()

	err := cmd.Run()
	if err != nil {
		if exitError, ok := err.(*exec.ExitError); ok {
			os.Exit(exitError.ExitCode())
		}
		fmt.Fprintf(os.Stderr, "Error running velune python engine: %v\n", err)
		os.Exit(1)
	}
}

func getPythonExec() string {
	// Try finding python3 or python in PATH
	if path, err := exec.LookPath("python3"); err == nil {
		return path
	}
	if path, err := exec.LookPath("python"); err == nil {
		return path
	}
	// Default fallback
	return "python"
}
