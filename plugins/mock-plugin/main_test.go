package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestProcessHandlerAddsMarker(t *testing.T) {
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

func TestHealthHandler(t *testing.T) {
	recorder := httptest.NewRecorder()
	healthHandler(recorder, httptest.NewRequest(http.MethodGet, "/health", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}
}
