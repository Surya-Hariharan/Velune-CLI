package main

import (
	"flag"
	"fmt"
	"log"
)

func main() {
	port := flag.Int("port", 48102, "Port for the daemon to listen on")
	maxMemoryMb := flag.Float64("max-memory", 1024.0, "Max memory in MB for child processes")
	maxOutputBytes := flag.Int("max-output", 10*1024*1024, "Max bytes to buffer for stdout/stderr")
	flag.Parse()

	sandbox := &Sandbox{
		MaxMemoryMb:    *maxMemoryMb,
		MaxOutputBytes: *maxOutputBytes,
	}

	addr := fmt.Sprintf("127.0.0.1:%d", *port)
	log.Printf("Starting Velune Daemon on %s", addr)
	log.Printf("Sandbox limits: Memory=%.1fMB, Output=%d bytes", *maxMemoryMb, *maxOutputBytes)

	if err := StartIPCServer(addr, sandbox); err != nil {
		log.Fatalf("Daemon server failed: %v", err)
	}
}
