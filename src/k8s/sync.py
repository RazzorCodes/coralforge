import json
import logging
import threading
import time
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from typing import Dict, Any, Optional

logger = logging.getLogger("coralforge.k8s")

class ConfigMapSync:
    """
    Handles syncing Python dictionaries to Kubernetes ConfigMaps.
    Useful for persisting state machine status outside of the pod lifecycle.
    """

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.v1: Optional[client.CoreV1Api] = None
        
        try:
            # Try in-cluster first, fallback to kubeconfig
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            
            self.v1 = client.CoreV1Api()
            logger.debug(f"Kubernetes client initialized for namespace: {namespace}")
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")

    def sync(self, name: str, data: Dict[str, Any]) -> bool:
        """
        Overwrites a ConfigMap with the provided dictionary.
        Values are stringified; complex types (dict, list) are JSON encoded.
        """
        if not self.v1:
            logger.error(f"Sync failed for {name}: Kubernetes client not available.")
            return False

        # Ensure all values are strings for K8s compatibility
        string_data = {}
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                string_data[str(k)] = json.dumps(v)
            else:
                string_data[str(k)] = str(v)

        metadata = client.V1ObjectMeta(name=name)
        cm = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=metadata,
            data=string_data
        )

        try:
            try:
                # Check if it exists to decide between replace or create
                self.v1.read_namespaced_config_map(name, self.namespace)
                self.v1.replace_namespaced_config_map(name, self.namespace, cm)
                logger.info(f"Updated ConfigMap: {self.namespace}/{name}")
            except ApiException as e:
                if e.status == 404:
                    self.v1.create_namespaced_config_map(self.namespace, cm)
                    logger.info(f"Created ConfigMap: {self.namespace}/{name}")
                else:
                    raise e
            return True
        except Exception as e:
            logger.error(f"Failed to sync ConfigMap {self.namespace}/{name}: {e}")
            return False

    def watch_config(self, name: str, callback):
        """
        Watches a specific ConfigMap and executes a callback whenever it changes.
        This is event-driven and does not poll.
        """
        if not self.v1:
            return

        def _run_watch():
            w = watch.Watch()
            while True:
                try:
                    # Watch specifically for our configmap
                    for event in w.stream(self.v1.list_namespaced_config_map, self.namespace, field_selector=f"metadata.name={name}"):
                        obj = event['object']
                        # Re-parse the data and trigger callback
                        raw_data = obj.data or {}
                        parsed_data = {}
                        for k, v in raw_data.items():
                            try:
                                if v.startswith(("{", "[")):
                                    parsed_data[k] = json.loads(v)
                                else:
                                    parsed_data[k] = v
                            except:
                                parsed_data[k] = v

                        logger.info(f"ConfigMap {name} changed ({event['type']}). Triggering reload.")
                        callback(parsed_data)
                except Exception as e:
                    logger.error(f"ConfigMap watch error: {e}. Restarting watch in 5s...")
                    time.sleep(5)

        thread = threading.Thread(target=_run_watch, daemon=True, name=f"ConfigWatch-{name}")
        thread.start()

