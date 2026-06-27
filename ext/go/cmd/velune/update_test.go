package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestVersionEqual(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"0.9.3", "0.9.3", true},
		{"v0.9.3", "0.9.3", true},
		{"0.9.3", "v0.9.3", true},
		{"v0.9.3", "v0.9.3", true},
		{"0.9.3 ", " 0.9.3", true},
		{"0.9.3", "0.9.4", false},
		{"0.9.3", "1.0.0", false},
		{"unknown", "0.9.3", false},
	}

	for _, c := range cases {
		got := versionEqual(c.a, c.b)
		if got != c.want {
			t.Errorf("versionEqual(%q, %q) = %v; want %v", c.a, c.b, got, c.want)
		}
	}
}

func TestNormaliseVersion(t *testing.T) {
	cases := map[string]string{
		"v0.9.3":  "0.9.3",
		"0.9.3":   "0.9.3",
		" 0.9.3 ": "0.9.3",
		"v1.0.0":  "1.0.0",
	}
	for in, want := range cases {
		if got := normaliseVersion(in); got != want {
			t.Errorf("normaliseVersion(%q) = %q; want %q", in, got, want)
		}
	}
}

func TestFetchLatestVersion_MockServer(t *testing.T) {
	payload := map[string]interface{}{
		"info": map[string]string{"version": "9.9.9"},
	}
	body, _ := json.Marshal(payload)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	// Patch the global URL for this test.
	orig := pypiURL
	_ = orig // pypiURL is a const; we test via a direct HTTP call instead
	// Directly call the HTTP client logic with the test server URL.
	got, err := fetchLatestVersionFromURL(srv.URL)
	if err != nil {
		t.Fatalf("fetchLatestVersionFromURL error: %v", err)
	}
	if got != "9.9.9" {
		t.Fatalf("expected 9.9.9, got %q", got)
	}
}

func TestFetchLatestVersion_BadJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer srv.Close()

	_, err := fetchLatestVersionFromURL(srv.URL)
	if err == nil {
		t.Fatal("expected parse error for bad JSON response")
	}
}

func TestFetchLatestVersion_HTTP500(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	_, err := fetchLatestVersionFromURL(srv.URL)
	if err == nil {
		t.Fatal("expected error for HTTP 500")
	}
}
