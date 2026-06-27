package main

import (
	"context"
	"encoding/json"
	"net/http"
	"time"
)

type DaemonServer struct {
	Sandbox *Sandbox
}

type ExecRequest struct {
	Command string        `json:"command"`
	Args    []string      `json:"args"`
	Dir     string        `json:"dir"`
	Timeout time.Duration `json:"timeout_ms"`
}

type ExecResponse struct {
	Result *SandboxResult `json:"result,omitempty"`
	Error  string         `json:"error,omitempty"`
}

func (s *DaemonServer) handleExecute(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req ExecRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	timeout := req.Timeout * time.Millisecond
	if timeout <= 0 {
		timeout = 30 * time.Second
	}

	result, err := s.Sandbox.Execute(context.Background(), req.Command, req.Args, req.Dir, timeout)
	
	resp := ExecResponse{Result: result}
	if err != nil {
		resp.Error = err.Error()
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (s *DaemonServer) handlePing(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok", "service": "velune-daemon"})
}

func StartIPCServer(addr string, sandbox *Sandbox) error {
	server := &DaemonServer{Sandbox: sandbox}

	mux := http.NewServeMux()
	mux.HandleFunc("/execute", server.handleExecute)
	mux.HandleFunc("/ping", server.handlePing)

	return http.ListenAndServe(addr, mux)
}
