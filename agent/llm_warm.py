"""Claude CLI wrapper — uses shell=True to get proper PATH and auth."""

import logging
import shlex
import subprocess
import threading
import time

logger = logging.getLogger("agent")


class WarmClaude:
    def __init__(self, model: str = "haiku", timeout: float = 50):
        self.model = model
        self.timeout = timeout
        self._lock = threading.Lock()

    def call(self, prompt: str) -> tuple[str, float]:
        """Send prompt, get response. Returns (text, latency_ms)."""
        with self._lock:
            escaped = shlex.quote(prompt)
            cmd = f"claude -p --model {self.model} {escaped}"

            start = time.perf_counter()
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                text = result.stdout.strip()
                if not text and result.stderr:
                    logger.warning("Claude stderr: %.100s", result.stderr.strip())
                return text, elapsed_ms

            except subprocess.TimeoutExpired:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.warning("Claude timed out (%.0fms)", elapsed_ms)
                return "", elapsed_ms

    def close(self):
        pass


_instance: WarmClaude | None = None


def get_warm_claude(model: str = "haiku", timeout: float = 50) -> WarmClaude:
    global _instance
    if _instance is None:
        _instance = WarmClaude(model=model, timeout=timeout)
    return _instance
