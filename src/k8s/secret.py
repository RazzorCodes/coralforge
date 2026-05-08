import logging
import threading
import base64
import time
from kubernetes import client, config, watch
from typing import Dict, Any, Optional, Tuple, Set

logger = logging.getLogger("coralforge.secrets")

class SecretSync:
    """
    Manages a live cache of Kubernetes Secrets.
    Uses background watches to ensure values are 'constantly synced' with the cluster.
    """

    def __init__(self):
        self._cache: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watched_namespaces: Set[str] = set()
        
        try:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self.v1 = client.CoreV1Api()
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client for SecretSync: {e}")
            self.v1 = None

    def get_value(self, namespace: str, name: str, key: str) -> Optional[str]:
        """
        Retrieves a secret value from the cache. 
        If not cached, performs a direct read and ensures the namespace is being watched.
        """
        if not self.v1:
            return None

        # Ensure we are watching this namespace for future updates
        if namespace not in self._watched_namespaces:
            self.start_watching(namespace)

        with self._lock:
            secret_data = self._cache.get((namespace, name))
            if secret_data and key in secret_data:
                return secret_data[key]

        # Fallback to direct read if cache miss
        return self._direct_read(namespace, name, key)

    def _direct_read(self, namespace: str, name: str, key: str) -> Optional[str]:
        try:
            secret = self.v1.read_namespaced_secret(name, namespace)
            data = self._decode_secret(secret)
            with self._lock:
                self._cache[(namespace, name)] = data
            return data.get(key)
        except Exception as e:
            logger.error(f"Failed to read secret {namespace}/{name} directly: {e}")
            return None

    def _decode_secret(self, secret) -> Dict[str, str]:
        decoded = {}
        if secret.data:
            for k, v in secret.data.items():
                try:
                    # K8s secret values are base64 encoded strings
                    decoded[k] = base64.b64decode(v).decode('utf-8')
                except Exception:
                    decoded[k] = v
        return decoded

    def start_watching(self, namespace: str):
        """Starts a background watch for a namespace if not already watching."""
        with self._lock:
            if namespace in self._watched_namespaces:
                return
            self._watched_namespaces.add(namespace)
        
        logger.info(f"Starting background sync for secrets in namespace: {namespace}")
        thread = threading.Thread(
            target=self._watch_loop, 
            args=(namespace,), 
            daemon=True,
            name=f"SecretWatch-{namespace}"
        )
        thread.start()

    def _watch_loop(self, namespace: str):
        w = watch.Watch()
        while not self._stop_event.is_set():
            try:
                # stream() handles the connection and reconnects on timeout
                for event in w.stream(self.v1.list_namespaced_secret, namespace, timeout_seconds=300):
                    if self._stop_event.is_set():
                        break
                        
                    obj = event['object']
                    name = obj.metadata.name
                    event_type = event['type']

                    if event_type in ("ADDED", "MODIFIED"):
                        data = self._decode_secret(obj)
                        with self._lock:
                            self._cache[(namespace, name)] = data
                        logger.debug(f"Secret {namespace}/{name} synced ({event_type})")
                    
                    elif event_type == "DELETED":
                        with self._lock:
                            self._cache.pop((namespace, name), None)
                        logger.debug(f"Secret {namespace}/{name} removed from cache")

            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"Secret watch error in {namespace}: {e}. Retrying in 5s...")
                    time.sleep(5)

    def stop(self):
        """Stops all background watches."""
        self._stop_event.set()
