import json
import os
from pathlib import Path
from typing import List, Optional

from assisted_service_client import models
from junit_report import JunitTestCase

import consts
from assisted_test_infra.test_infra import BaseInfraEnvConfig, utils
from assisted_test_infra.test_infra.helper_classes.entity import Entity
from assisted_test_infra.test_infra.helper_classes.nodes import Nodes
from assisted_test_infra.test_infra.tools import static_network
from assisted_test_infra.test_infra.utils.waiting import wait_till_all_infra_env_hosts_are_in_status
from service_client import InventoryClient, log


class InfraEnv(Entity):
    _config: BaseInfraEnvConfig

    def __init__(self, api_client: InventoryClient, config: BaseInfraEnvConfig, nodes: Optional[Nodes] = None):
        super().__init__(api_client, config, nodes)

    @property
    def id(self):
        return self._config.infra_env_id

    def update_existing(self) -> str:
        # Might cause issues with defaults
        self.api_client.update_infra_env(self.id, models.InfraEnvUpdateParams(image_type=self._config.iso_image_type))
        return self.id

    def _create(self):
        if self._config.ignition_config_override:
            ignition_config_override = json.dumps(self._config.ignition_config_override)
        else:
            ignition_config_override = None

        infraenv_create_params = dict(
            pull_secret=self._config.pull_secret,
            ssh_public_key=self._config.ssh_public_key,
            openshift_version=self._config.openshift_version,
            cluster_id=self._config.cluster_id,
            static_network_config=self._config.static_network_config,
            ignition_config_override=ignition_config_override,
            proxy=self._config.proxy,
            image_type=self._config.iso_image_type,
        )
        if self._config.kernel_arguments is not None:
            infraenv_create_params["kernel_arguments"] = self._config.kernel_arguments
        if self._config.cpu_architecture:
            infraenv_create_params["cpu_architecture"] = self._config.cpu_architecture

        infra_env = self.api_client.create_infra_env(self._config.entity_name.get(), **infraenv_create_params)
        self._config.infra_env_id = infra_env.id
        return infra_env.id

    def get_iso_download_path(self, iso_download_path: str = None):
        return iso_download_path or self._config.iso_download_path

    @JunitTestCase()
    def download_image(self, iso_download_path: str = None) -> Path:
        iso_download_url = self.get_details().download_url
        iso_download_path = self.get_iso_download_path(iso_download_path)

        # ensure file path exists before downloading
        if not os.path.exists(iso_download_path):
            utils.recreate_folder(os.path.dirname(iso_download_path), force_recreate=False)

        self._update_static_network()

        self.nodes.get_controller().set_download_path(iso_download_path)

        log.info(f"Downloading image {iso_download_url} to {iso_download_path}")
        return utils.download_file(iso_download_url, iso_download_path, self._config.verify_download_iso_ssl)

    @JunitTestCase()
    def download_infra_env_file(self, file_name: str, file_path: str) -> Path:
        log.info(f"Downloading file {file_name} to {file_path}")

        # ensure file path exists before downloading
        if not os.path.exists(file_path):
            utils.recreate_folder(os.path.dirname(file_path), force_recreate=False)

        self.api_client.download_and_save_infra_env_file(self.id, file_name, file_path)

    @JunitTestCase()
    def wait_until_hosts_are_discovered(self, nodes_count: int, allow_insufficient=False):
        statuses = [consts.NodesStatus.KNOWN_UNBOUND]
        if allow_insufficient:
            statuses.append(consts.NodesStatus.INSUFFICIENT_UNBOUND)
        wait_till_all_infra_env_hosts_are_in_status(
            client=self.api_client,
            infra_env_id=self.id,
            nodes_count=nodes_count,
            statuses=statuses,
            timeout=consts.NODES_REGISTERED_TIMEOUT,
        )

    def update_host(
        self,
        host_id: str,
        host_role: Optional[str] = None,
        host_name: Optional[str] = None,
        node_labels: Optional[List[dict]] = None,
        disks_skip_formatting: Optional[List[dict]] = None,
    ):
        self.api_client.update_host(
            infra_env_id=self.id,
            host_id=host_id,
            host_role=host_role,
            host_name=host_name,
            node_labels=node_labels,
            disks_skip_formatting=disks_skip_formatting,
        )

    def update_host_installer_args(self, host_id: str):
        if not self._config.host_installer_args:
            return

        log.info(f"Updating host {host_id} with installer args: {self._config.host_installer_args}")
        self.api_client.client.v2_update_host_installer_args(
            infra_env_id=self.id, host_id=host_id, installer_args_params=self._config.host_installer_args
        )

    def bind_host(self, host_id: str, cluster_id: str) -> None:
        self.api_client.bind_host(infra_env_id=self.id, host_id=host_id, cluster_id=cluster_id)

    def unbind_host(self, host_id: str) -> None:
        self.api_client.unbind_host(infra_env_id=self.id, host_id=host_id)

    def delete_host(self, host_id: str) -> None:
        self.api_client.deregister_host(infra_env_id=self.id, host_id=host_id)

    def get_discovery_ignition(self) -> str:
        return self.api_client.get_discovery_ignition(infra_env_id=self.id)

    def patch_discovery_ignition(self, ignition_info: str) -> None:
        self.api_client.patch_discovery_ignition(infra_env_id=self.id, ignition_info=ignition_info)

    def get_details(self) -> models.infra_env.InfraEnv:
        return self.api_client.get_infra_env(infra_env_id=self.id)

    def update_proxy(self, proxy: models.Proxy) -> None:
        self.update_config(proxy=proxy)
        infra_env_update_params = models.InfraEnvUpdateParams(proxy=self._config.proxy)
        self.api_client.update_infra_env(infra_env_id=self.id, infra_env_update_params=infra_env_update_params)

    def _update_static_network(self):
        if not self._config.is_static_ip:
            return

        if self._config.static_network_config is None:
            log.info("No static network configuration found, generating new network configurations")
            static_network_config = static_network.generate_static_network_data_from_tf(
                self.nodes.controller.tf_folder, self._config
            )
            self._config.static_network_config = static_network_config

        log.info(f"Updating InfraEnv {self.id} static network configuration")
        infra_env_update_params = models.InfraEnvUpdateParams(static_network_config=self._config.static_network_config)
        self.api_client.update_infra_env(infra_env_id=self.id, infra_env_update_params=infra_env_update_params)
        log.info(f"InfraEnv static network configuration successfully updated {self._config.static_network_config}")

    def update_infra_env_kernel(self, kernel_arguments: List[dict[str, str]]) -> None:
        self.update_config(kernel_arguments=kernel_arguments)
        infra_env_update_params = models.InfraEnvUpdateParams(kernel_arguments=kernel_arguments)
        self.api_client.update_infra_env(infra_env_id=self.id, infra_env_update_params=infra_env_update_params)

    def update_static_network_config(self, static_network_config: List[dict]) -> None:
        self.update_config(static_network_config=static_network_config)
        infra_env_update_params = models.InfraEnvUpdateParams(static_network_config=static_network_config)
        self.api_client.update_infra_env(infra_env_id=self.id, infra_env_update_params=infra_env_update_params)

    def select_host_installation_disk(self, host_id: str, disk_paths: List[dict]) -> None:
        self.api_client.select_installation_disk(infra_env_id=self.id, host_id=host_id, disk_paths=disk_paths)

    def deregister(self, deregister_hosts=True):
        log.info(f"Deregister infra env with id: {self.id}")
        if deregister_hosts:
            for host in self.api_client.client.v2_list_hosts(self.id):
                log.info(f"Deregister infra_env host with id: {host['id']}")
                self.api_client.client.v2_deregister_host(infra_env_id=self.id, host_id=host["id"])

        self.api_client.client.deregister_infra_env(self.id)
        self._config.infra_env_id = None
