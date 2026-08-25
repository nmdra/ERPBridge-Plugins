package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestProcessHandlerAddsMarker(t *testing.T) {
	t.Setenv(mockPluginAPIKeyEnv, "")
	payload := `{"protocolVersion":"v1","invocationId":"id","tool":{"name":"fixture","version":"1.0.0"},"result":{"state":"source"},"config":{"mode":"test"}}`
	request := httptest.NewRequest(http.MethodPost, "/v1/process", strings.NewReader(payload))
	recorder := httptest.NewRecorder()
	processHandler(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	result, ok := response["result"].(map[string]any)
	if !ok || result["processedBy"] != "mock-plugin" || result["state"] != "source" {
		t.Fatalf("unexpected response: %#v", response)
	}
}

func TestProcessHandlerRequiresAPIKey(t *testing.T) {
	const configuredKey = "expected-plugin-key"
	const payload = `{"protocolVersion":"v1","invocationId":"id","tool":{"name":"fixture","version":"1.0.0"},"result":{"state":"source"}}`
	t.Setenv(mockPluginAPIKeyEnv, configuredKey)

	for _, test := range []struct {
		name   string
		key    string
		status int
	}{
		{name: "missing", status: http.StatusUnauthorized},
		{name: "wrong", key: "wrong-plugin-key", status: http.StatusUnauthorized},
		{name: "correct", key: configuredKey, status: http.StatusOK},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "/v1/process", strings.NewReader(payload))
			if test.key != "" {
				request.Header.Set(mockPluginAPIKeyHeader, test.key)
			}
			recorder := httptest.NewRecorder()
			processHandler(recorder, request)
			if recorder.Code != test.status {
				t.Fatalf("status = %d, want %d", recorder.Code, test.status)
			}
		})
	}
}

func TestHealthHandler(t *testing.T) {
	t.Setenv(mockPluginAPIKeyEnv, "configured-plugin-key")
	recorder := httptest.NewRecorder()
	healthHandler(recorder, httptest.NewRequest(http.MethodGet, "/health", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}
}
