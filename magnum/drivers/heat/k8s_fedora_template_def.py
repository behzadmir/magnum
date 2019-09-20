# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_log import log as logging
from oslo_utils import strutils

from magnum.common import exception
from magnum.common.x509 import operations as x509
from magnum.conductor.handlers.common import cert_manager
from magnum.drivers.heat import k8s_template_def
from magnum.drivers.heat import template_def
from magnum.i18n import _
from oslo_config import cfg
import six

CONF = cfg.CONF

LOG = logging.getLogger(__name__)


class ServerAddressOutputMapping(template_def.OutputMapping):

    public_ip_output_key = None
    private_ip_output_key = None

    def __init__(self, dummy_arg, cluster_attr=None):
        self.cluster_attr = cluster_attr
        self.heat_output = self.public_ip_output_key

    def set_output(self, stack, cluster_template, cluster):
        if not cluster_template.floating_ip_enabled:
            self.heat_output = self.private_ip_output_key

        LOG.debug("Using heat_output: %s", self.heat_output)
        super(ServerAddressOutputMapping,
              self).set_output(stack, cluster_template, cluster)


class MasterAddressOutputMapping(ServerAddressOutputMapping):
    public_ip_output_key = 'kube_masters'
    private_ip_output_key = 'kube_masters_private'


class NodeAddressOutputMapping(ServerAddressOutputMapping):
    public_ip_output_key = 'kube_minions'
    private_ip_output_key = 'kube_minions_private'


class K8sFedoraTemplateDefinition(k8s_template_def.K8sTemplateDefinition):
    """Kubernetes template for a Fedora."""

    def __init__(self):
        super(K8sFedoraTemplateDefinition, self).__init__()
        self.add_parameter('docker_volume_size',
                           cluster_attr='docker_volume_size')
        self.add_parameter('docker_storage_driver',
                           cluster_template_attr='docker_storage_driver')
        self.add_output('kube_minions',
                        cluster_attr='node_addresses',
                        mapping_type=NodeAddressOutputMapping)
        self.add_output('kube_masters',
                        cluster_attr='master_addresses',
                        mapping_type=MasterAddressOutputMapping)

    def get_params(self, context, cluster_template, cluster, **kwargs):
        extra_params = kwargs.pop('extra_params', {})

        extra_params['username'] = context.user_name
        osc = self.get_osc(context)
        extra_params['region_name'] = osc.cinder_region_name()

        # set docker_volume_type
        # use the configuration default if None provided
        docker_volume_type = cluster.labels.get(
            'docker_volume_type', CONF.cinder.default_docker_volume_type)
        extra_params['docker_volume_type'] = docker_volume_type

        extra_params['nodes_affinity_policy'] = \
            CONF.cluster.nodes_affinity_policy

        if cluster_template.network_driver == 'flannel':
            extra_params["pods_network_cidr"] = \
                cluster.labels.get('flannel_network_cidr', '10.100.0.0/16')
        if cluster_template.network_driver == 'calico':
            extra_params["pods_network_cidr"] = \
                cluster.labels.get('calico_ipv4pool', '192.168.0.0/16')

        # check cloud provider and cinder options. If cinder is selected,
        # the cloud provider needs to be enabled.
        cloud_provider_enabled = cluster.labels.get(
            'cloud_provider_enabled',
            'true' if CONF.trust.cluster_user_trust else 'false')
        if (not CONF.trust.cluster_user_trust
                and cloud_provider_enabled.lower() == 'true'):
            raise exception.InvalidParameterValue(_(
                '"cluster_user_trust" must be set to True in magnum.conf when '
                '"cloud_provider_enabled" label is set to true.'))
        if (cluster_template.volume_driver == 'cinder'
                and cloud_provider_enabled.lower() == 'false'):
            raise exception.InvalidParameterValue(_(
                '"cinder" volume driver needs "cloud_provider_enabled" label '
                'to be true or unset.'))
        extra_params['cloud_provider_enabled'] = cloud_provider_enabled

        label_list = ['kube_tag', 'container_infra_prefix',
                      'availability_zone', 'cgroup_driver',
                      'calico_tag', 'calico_cni_tag',
                      'calico_kube_controllers_tag', 'calico_ipv4pool',
                      'etcd_tag', 'flannel_tag', 'flannel_cni_tag',
                      'cloud_provider_tag',
                      'prometheus_tag', 'grafana_tag',
                      'heat_container_agent_tag',
                      'keystone_auth_enabled', 'k8s_keystone_auth_tag',
                      'monitoring_enabled',
                      'tiller_enabled',
                      'tiller_tag',
                      'tiller_namespace',
                      'node_problem_detector_tag',
                      'auto_healing_enabled', 'auto_scaling_enabled',
                      'draino_tag', 'autoscaler_tag',
                      'min_node_count', 'max_node_count',
                      'nginx_ingress_controller_tag']

        for label in label_list:
            label_value = cluster.labels.get(label)
            if label_value:
                extra_params[label] = label_value

        csr_keys = x509.generate_csr_and_key(u"Kubernetes Service Account")

        extra_params['kube_service_account_key'] = \
            csr_keys["public_key"].replace("\n", "\\n")
        extra_params['kube_service_account_private_key'] = \
            csr_keys["private_key"].replace("\n", "\\n")

        extra_params['project_id'] = cluster.project_id

        if not extra_params.get('max_node_count'):
            extra_params['max_node_count'] = cluster.node_count + 1

        self._set_cert_manager_params(cluster, extra_params)

        return super(K8sFedoraTemplateDefinition,
                     self).get_params(context, cluster_template, cluster,
                                      extra_params=extra_params,
                                      **kwargs)

    def _set_cert_manager_params(self, cluster, extra_params):
        cert_manager_api = cluster.labels.get('cert_manager_api')
        if strutils.bool_from_string(cert_manager_api):
            extra_params['cert_manager_api'] = cert_manager_api
            ca_cert = cert_manager.get_cluster_ca_certificate(cluster)
            if six.PY3 and isinstance(ca_cert.get_private_key_passphrase(),
                                      six.text_type):
                extra_params['ca_key'] = x509.decrypt_key(
                    ca_cert.get_private_key(),
                    ca_cert.get_private_key_passphrase().encode()
                ).decode().replace("\n", "\\n")
            else:
                extra_params['ca_key'] = x509.decrypt_key(
                    ca_cert.get_private_key(),
                    ca_cert.get_private_key_passphrase()).replace("\n", "\\n")

    def get_env_files(self, cluster_template, cluster):
        env_files = []

        template_def.add_priv_net_env_file(env_files, cluster_template)
        template_def.add_etcd_volume_env_file(env_files, cluster_template)
        template_def.add_volume_env_file(env_files, cluster)
        template_def.add_lb_env_file(env_files, cluster_template)
        template_def.add_fip_env_file(env_files, cluster_template, cluster)

        return env_files
