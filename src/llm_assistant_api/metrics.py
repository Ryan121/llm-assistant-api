"""Simple metrics collection for the LLM Assistant API."""

import time
from collections import defaultdict
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class RequestMetrics:
    """Tracks metrics for individual requests."""
    request_id: str
    endpoint: str
    model: str
    start_time: float
    response_time_ms: Optional[float] = None
    status_code: Optional[int] = None
    streamed: bool = False
    
    def finish(self, status_code: int):
        """Mark request as finished."""
        self.response_time_ms = (time.perf_counter() - self.start_time) * 1000
        self.status_code = status_code

class MetricsCollector:
    """Collects and aggregates metrics for the application."""
    
    def __init__(self):
        self._requests: Dict[str, RequestMetrics] = {}
        self._endpoint_counts: defaultdict[str, int] = defaultdict(int)
        self._status_code_counts: defaultdict[int, int] = defaultdict(int)
        self._model_usage: defaultdict[str, int] = defaultdict(int)
        self._total_requests = 0
        
    def start_request(self, request_id: str, endpoint: str, model: str, streamed: bool = False) -> RequestMetrics:
        """Start tracking a new request."""
        metrics = RequestMetrics(
            request_id=request_id,
            endpoint=endpoint,
            model=model,
            start_time=time.perf_counter(),
            streamed=streamed
        )
        self._requests[request_id] = metrics
        self._total_requests += 1
        self._endpoint_counts[endpoint] += 1
        return metrics
        
    def finish_request(self, request_id: str, status_code: int):
        """Finish tracking a request."""
        if request_id in self._requests:
            self._requests[request_id].finish(status_code)
            self._status_code_counts[status_code] += 1
            # Clean up completed request
            del self._requests[request_id]
            
    def record_model_usage(self, model: str):
        """Record usage of a specific model."""
        self._model_usage[model] += 1
        
    def get_summary(self) -> dict:
        """Get a summary of collected metrics."""
        return {
            "total_requests": self._total_requests,
            "endpoint_counts": dict(self._endpoint_counts),
            "status_code_counts": dict(self._status_code_counts),
            "model_usage": dict(self._model_usage),
            "active_requests": len(self._requests),
            "timestamp": datetime.utcnow().isoformat()
        }
        
    def get_request_stats(self) -> dict:
        """Get statistics about recent requests."""
        if not self._requests:
            return {"average_response_time_ms": 0, "active_requests": 0}
            
        response_times = [req.response_time_ms for req in self._requests.values() if req.response_time_ms]
        if not response_times:
            return {"average_response_time_ms": 0, "active_requests": len(self._requests)}
            
        return {
            "average_response_time_ms": sum(response_times) / len(response_times),
            "active_requests": len(self._requests),
            "max_response_time_ms": max(response_times) if response_times else 0,
            "min_response_time_ms": min(response_times) if response_times else 0
        }

# Global metrics collector instance
metrics_collector = MetricsCollector()