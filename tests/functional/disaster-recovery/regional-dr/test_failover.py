import logging
from time import sleep

import pytest

from ocs_ci.framework import config
from ocs_ci.framework.pytest_customization.marks import rdr, turquoise_squad
from ocs_ci.framework.testlib import acceptance, tier1
from ocs_ci.helpers import dr_helpers
from ocs_ci.helpers.dr_helpers import (
    wait_for_replication_destinations_creation,
    wait_for_replication_destinations_deletion,
)
from ocs_ci.helpers.dr_helpers_ui import (
    dr_submariner_validation_from_ui,
    check_cluster_status_on_acm_console,
    failover_relocate_ui,
    verify_failover_relocate_status_ui,
)
from ocs_ci.ocs import constants
from ocs_ci.ocs.acm.acm import AcmAddClusters
from ocs_ci.ocs.node import wait_for_nodes_status, get_node_objs
from ocs_ci.ocs.resources.pod import wait_for_pods_to_be_running
from ocs_ci.utility.utils import ceph_health_check

logger = logging.getLogger(__name__)


@rdr
@acceptance
@tier1
@turquoise_squad
class TestFailover:
    """
    Test Failover action via CLI and UI

    """

    params = [
        pytest.param(
            False,  # primary_cluster_down = False
            constants.CEPHBLOCKPOOL,
            False,  # via_ui = False
            id="primary_up-rbd-cli",
            marks=pytest.mark.polarion_id("OCS-4429"),
        ),
        pytest.param(
            True,  # primary_cluster_down = True
            constants.CEPHBLOCKPOOL,
            False,  # via_ui = False
            marks=pytest.mark.polarion_id("OCS-4426"),
            id="primary_down-rbd-cli",
        ),
        pytest.param(
            False,  # primary_cluster_down = False
            constants.CEPHFILESYSTEM,
            False,  # via_ui = False
            marks=pytest.mark.polarion_id("OCS-4726"),
            id="primary_up-cephfs-cli",
        ),
        pytest.param(
            True,  # primary_cluster_down = True
            constants.CEPHFILESYSTEM,
            False,  # via_ui = False
            marks=pytest.mark.polarion_id("OCS-4729"),
            id="primary_down-cephfs-cli",
        ),
        pytest.param(
            False,  # primary_cluster_down = False
            constants.CEPHBLOCKPOOL,
            True,  # via_ui = True
            id="primary_up-rbd-ui",
            marks=pytest.mark.polarion_id("OCS-4741"),
        ),
        pytest.param(
            True,  # primary_cluster_down = True
            constants.CEPHBLOCKPOOL,
            True,  # via_ui = True
            marks=pytest.mark.polarion_id("OCS-4742"),
            id="primary_down-rbd-ui",
        ),
        pytest.param(
            False,  # primary_cluster_down = False
            constants.CEPHFILESYSTEM,
            True,  # via_ui = True
            marks=pytest.mark.polarion_id("OCS-6848"),
            id="primary_up-cephfs-ui",
        ),
        pytest.param(
            True,  # primary_cluster_down = True
            constants.CEPHFILESYSTEM,
            True,  # via_ui = True
            marks=pytest.mark.polarion_id("OCS-6847"),
            id="primary_down-cephfs-ui",
        ),
    ]

    @pytest.mark.parametrize(
        argnames=["primary_cluster_down", "pvc_interface", "via_ui"], argvalues=params
    )
    def test_failover(
        self,
        primary_cluster_down,
        pvc_interface,
        via_ui,
        setup_acm_ui,
        dr_workload,
        nodes_multicluster,
        node_restart_teardown,
    ):
        """
        Tests to verify application failover between managed clusters when the primary cluster is either UP or DOWN.

        This test will run twice both via CLI and UI

        """
        if via_ui:
            acm_obj = AcmAddClusters()

        workloads = dr_workload(
            num_of_subscription=1, num_of_appset=1, pvc_interface=pvc_interface
        )

        primary_cluster_name = dr_helpers.get_current_primary_cluster_name(
            workloads[0].workload_namespace, workloads[0].workload_type
        )
        config.switch_to_cluster_by_name(primary_cluster_name)
        primary_cluster_index = config.cur_index
        primary_cluster_nodes = get_node_objs()
        secondary_cluster_name = dr_helpers.get_current_secondary_cluster_name(
            workloads[0].workload_namespace, workloads[0].workload_type
        )

        if pvc_interface == constants.CEPHFILESYSTEM:
            # Verify the creation of ReplicationDestination resources on secondary cluster
            config.switch_to_cluster_by_name(secondary_cluster_name)
            for wl in workloads:
                wait_for_replication_destinations_creation(
                    wl.workload_pvc_count, wl.workload_namespace
                )

        scheduling_interval = dr_helpers.get_scheduling_interval(
            workloads[0].workload_namespace, workloads[0].workload_type
        )
        wait_time = 2 * scheduling_interval  # Time in minutes
        logger.info(f"Waiting for {wait_time} minutes to run IOs")
        sleep(wait_time * 60)

        if via_ui:
            logger.info("Start the process of Failover from ACM UI")
            config.switch_acm_ctx()
            dr_submariner_validation_from_ui(acm_obj)

        # Stop primary cluster nodes
        if primary_cluster_down:
            config.switch_to_cluster_by_name(primary_cluster_name)
            logger.info(f"Stopping nodes of primary cluster: {primary_cluster_name}")
            nodes_multicluster[primary_cluster_index].stop_nodes(primary_cluster_nodes)

            # Verify if cluster is marked unknown on ACM console
            if via_ui:
                config.switch_acm_ctx()
                check_cluster_status_on_acm_console(
                    acm_obj,
                    down_cluster_name=primary_cluster_name,
                    expected_text="Unknown",
                )
        elif via_ui:
            check_cluster_status_on_acm_console(acm_obj)

        for wl in workloads:
            if via_ui:
                # Failover via ACM UI
                failover_relocate_ui(
                    acm_obj,
                    scheduling_interval=scheduling_interval,
                    workload_to_move=f"{wl.workload_name}-1",
                    policy_name=wl.dr_policy_name,
                    failover_or_preferred_cluster=secondary_cluster_name,
                )
            else:
                # Failover action via CLI
                dr_helpers.failover(
                    secondary_cluster_name,
                    wl.workload_namespace,
                    wl.workload_type,
                    (
                        wl.appset_placement_name
                        if wl.workload_type == constants.APPLICATION_SET
                        else None
                    ),
                )

        # Verify resources creation on secondary cluster (failoverCluster)
        config.switch_to_cluster_by_name(secondary_cluster_name)
        for wl in workloads:
            dr_helpers.wait_for_all_resources_creation(
                wl.workload_pvc_count,
                wl.workload_pod_count,
                wl.workload_namespace,
            )

        # Verify resources deletion from primary cluster
        config.switch_to_cluster_by_name(primary_cluster_name)

        # Start nodes if cluster is down
        if primary_cluster_down:
            logger.info(
                f"Waiting for {wait_time} minutes before starting nodes of primary cluster: {primary_cluster_name}"
            )
            sleep(wait_time * 60)
            nodes_multicluster[primary_cluster_index].start_nodes(primary_cluster_nodes)
            wait_for_nodes_status([node.name for node in primary_cluster_nodes])
            logger.info("Wait for 180 seconds for pods to stabilize")
            sleep(180)
            logger.info(
                "Wait for all the pods in openshift-storage to be in running state"
            )
            assert wait_for_pods_to_be_running(
                timeout=720
            ), "Not all the pods reached running state"
            logger.info("Checking for Ceph Health OK")
            ceph_health_check()

        for wl in workloads:
            dr_helpers.wait_for_all_resources_deletion(wl.workload_namespace)

        if pvc_interface == constants.CEPHFILESYSTEM:
            for wl in workloads:
                # Verify the deletion of ReplicationDestination resources on secondary cluster
                config.switch_to_cluster_by_name(secondary_cluster_name)
                wait_for_replication_destinations_deletion(wl.workload_namespace)
                # Verify the creation of ReplicationDestination resources on primary cluster
                config.switch_to_cluster_by_name(primary_cluster_name)
                wait_for_replication_destinations_creation(
                    wl.workload_pvc_count, wl.workload_namespace
                )

        if pvc_interface == constants.CEPHBLOCKPOOL:
            dr_helpers.wait_for_mirroring_status_ok(
                replaying_images=sum([wl.workload_pvc_count for wl in workloads])
            )

        if via_ui:
            config.switch_acm_ctx()
            verify_failover_relocate_status_ui(acm_obj)
