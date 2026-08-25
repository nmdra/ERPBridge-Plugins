package main

import (
	"crypto/subtle"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"
)

const (
	maxJSONBytes           = 1 << 20
	mockPluginAPIKeyEnv    = "MOCK_PLUGIN_API_KEY"
	mockPluginAPIKeyHeader = "X-API-Key"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/v1/process", processHandler)
	server := &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	slog.Info("mock-plugin listening", slog.String("port", port))
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		slog.Error("mock-plugin stopped", slog.String("error", err.Error()))
		os.Exit(1)
	}
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func processHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}
	configuredKey := os.Getenv(mockPluginAPIKeyEnv)
	if configuredKey != "" && subtle.ConstantTimeCompare([]byte(r.Header.Get(mockPluginAPIKeyHeader)), []byte(configuredKey)) != 1 {
		http.Error(w, http.StatusText(http.StatusUnauthorized), http.StatusUnauthorized)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxJSONBytes+1))
	if err != nil || len(body) > maxJSONBytes {
		http.Error(w, http.StatusText(http.StatusRequestEntityTooLarge), http.StatusRequestEntityTooLarge)
		return
	}
	var invocation struct {
		Result any `json:"result"`
	}
	if err := json.Unmarshal(body, &invocation); err != nil {
		http.Error(w, http.StatusText(http.StatusBadRequest), http.StatusBadRequest)
		return
	}
	result, ok := invocation.Result.(map[string]any)
	if !ok {
		http.Error(w, "result must be a JSON object", http.StatusUnprocessableEntity)
		return
	}
	transformed := make(map[string]any, len(result)+1)
	for key, value := range result {
		transformed[key] = value
	}
	transformed["processedBy"] = "mock-plugin"
	writeJSON(w, http.StatusOK, map[string]any{"result": transformed})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
