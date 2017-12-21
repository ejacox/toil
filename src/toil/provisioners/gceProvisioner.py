# Copyright (C) 2015 UCSC Computational Genomics Lab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from builtins import str
from builtins import map
from builtins import range

import os
import sys
import pipes
import subprocess
import time
import threading
import json
import requests

from libcloud.compute.types import Provider
from libcloud.compute.providers import get_driver

from toil import applianceSelf
from toil.provisioners.abstractProvisioner import AbstractProvisioner, Shape
from toil.provisioners import (Node, NoSuchClusterException)

import logging
logger = logging.getLogger(__name__)

## SECURITY
# 1. Google Service Account (json file)
#   - Gives access to the driver.
#   - Location read from GOOGLE_APPLICATION_CREDENTIALS
#   - Not necessary from a Google instance (TODO: CHECK THIS)
#       - Just needed for Toil cluster commands.
# 2. ssh key
#   - Add keys to the service account on the Google console
#   - Automatically inserted into the instance (root).
#   - the keyName input parameter indicates which key this is
#   - It is not copied to the core user in the appliance (TODO: Why not)
#       - copy expressly in _copySshKeys()
#   - Used in waitForNode (ssh commands), ssh and rysnc
#   - TODO: can I change this to ssh with the SA account?
# 3. Jobstore access
#   - other credentials might be necessary to access jobStore
#   - TODO: do gs by default
#   - copy .boto for AWS (currently done with 'toil rysnc-cluster --workersToo ...'



# - Rename works
#   - Is there a way to cluster in GCE?
#   - TEST BY ADDING NODES TWICE FROM LAUNCH CLUSTER
#   - SOLUTION
#      1. Use unmanaged instance groups: add, get all, delete all
#      2. Add uuid for workers (still use create-multiple)
# - tests working
# - check changes outside of this class (e.g. rsync call)
#       git diff master
# - instructions (on github?)
# - gce jobStore
# - set and test vpc-subnet
# - preemptable


# TODO
# - libcloud ex_create_multiple_nodes bug that doesn't allow multi nodes
#   - submit issue
# - revisit ssh keys
#   - cloud config?
#   - This error: Failed Units: 1 coreos-metadata-sshkeys@core.service
#   - try passing keyName to coreSSH instead; then no need to copy authorized keys
# - security (sse) keys
#   - test encryption
#   - keyName is needed to copy ssh key
# - advanced
#   - other disks
#   - RAID

logger = logging.getLogger(__name__)

logDir = '--log_dir=/var/lib/mesos'
leaderDockerArgs = logDir + ' --registry=in_memory --cluster={name}'
workerDockerArgs = '{keyPath} --work_dir=/var/lib/mesos --master={ip}:5050 --attributes=preemptable:{preemptable} ' + logDir
gceUserData = """#cloud-config

write_files:
    - path: "/home/core/volumes.sh"
      permissions: "0777"
      owner: "root"
      content: |
        #!/bin/bash
        set -x
        ephemeral_count=0
        drives=""
        directories="toil mesos docker"
        for drive in /dev/xvd{{b..z}}; do
            echo checking for $drive
            if [ -b $drive ]; then
                echo found it
                ephemeral_count=$((ephemeral_count + 1 ))
                drives="$drives $drive"
                echo increased ephemeral count by one
            fi
        done
        if (("$ephemeral_count" == "0" )); then
            echo no ephemeral drive
            for directory in $directories; do
                sudo mkdir -p /var/lib/$directory
            done
            exit 0
        fi
        sudo mkdir /mnt/ephemeral
        if (("$ephemeral_count" == "1" )); then
            echo one ephemeral drive to mount
            sudo mkfs.ext4 -F $drives
            sudo mount $drives /mnt/ephemeral
        fi
        if (("$ephemeral_count" > "1" )); then
            echo multiple drives
            for drive in $drives; do
                dd if=/dev/zero of=$drive bs=4096 count=1024
            done
            sudo mdadm --create -f --verbose /dev/md0 --level=0 --raid-devices=$ephemeral_count $drives # determine force flag
            sudo mkfs.ext4 -F /dev/md0
            sudo mount /dev/md0 /mnt/ephemeral
        fi
        for directory in $directories; do
            sudo mkdir -p /mnt/ephemeral/var/lib/$directory
            sudo mkdir -p /var/lib/$directory
            sudo mount --bind /mnt/ephemeral/var/lib/$directory /var/lib/$directory
        done

coreos:
    update:
      reboot-strategy: off
    units:
    - name: "volume-mounting.service"
      command: "start"
      content: |
        [Unit]
        Description=mounts ephemeral volumes & bind mounts toil directories
        Author=cketchum@ucsc.edu
        Before=docker.service

        [Service]
        Type=oneshot
        Restart=no
        ExecStart=/usr/bin/bash /home/core/volumes.sh

    - name: "toil-{role}.service"
      command: "start"
      content: |
        [Unit]
        Description=toil-{role} container
        Author=cketchum@ucsc.edu
        After=docker.service

        [Service]
        Restart=on-failure
        RestartSec=2
        ExecPre=-/usr/bin/docker rm toil_{role}
        ExecStart=/usr/bin/docker run \
            --entrypoint={entrypoint} \
            --net=host \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v /var/lib/mesos:/var/lib/mesos \
            -v /var/lib/docker:/var/lib/docker \
            -v /var/lib/toil:/var/lib/toil \
            -v /var/lib/cwl:/var/lib/cwl \
            -v /tmp:/tmp \
            --name=toil_{role} \
            {dockerImage} \
            {dockerArgs}
    - name: "node-exporter.service"
      command: "start"
      content: |
        [Unit]
        Description=node-exporter container
        After=docker.service

        [Service]
        Restart=on-failure
        RestartSec=2
        ExecPre=-/usr/bin/docker rm node_exporter
        ExecStart=/usr/bin/docker run \
            -p 9100:9100 \
            -v /proc:/host/proc \
            -v /sys:/host/sys \
            -v /:/rootfs \
            --name node-exporter \
            --restart always \
            prom/node-exporter:0.12.0 \
            -collector.procfs /host/proc \
            -collector.sysfs /host/sys \
            -collector.filesystem.ignored-mount-points ^/(sys|proc|dev|host|etc)($|/)
"""

gceUserDataWithSsh = gceUserData + """
ssh_authorized_keys:
    - "ssh-rsa {sshKey}"
"""

nodeBotoPath = "/root/.boto"

class GCEProvisioner(AbstractProvisioner):

    def __init__(self, config=None):
        """
        :param config: Optional config object from common.py
        :param batchSystem:
        """
        super(GCEProvisioner, self).__init__(config)

        # TODO: zone should be set in the constructor, not in the various calls

        # From a GCE instance, these values can be blank. Only the projectId is needed
        self.googleJson = ''
        self.clientEmail = ''

        if config:
            # https://cloud.google.com/compute/docs/storing-retrieving-metadata
            metadata_server = "http://metadata/computeMetadata/v1/instance/"
            metadata_flavor = {'Metadata-Flavor' : 'Google'}
            self.clusterName = requests.get(metadata_server + 'name', headers = metadata_flavor).text
            self.zone = requests.get(metadata_server + 'zone', headers = metadata_flavor).text
            self.zone = self.zone.split('/')[-1]

            project_metadata_server = "http://metadata/computeMetadata/v1/project/"
            self.projectId = requests.get(project_metadata_server + 'project-id', headers = metadata_flavor).text

            leader = self._getLeader(self.clusterName)
            self.tags = leader.extra['description']
            tags = json.loads(self.tags)
            self.leaderIP = leader.private_ips[0]  # this is PRIVATE IP
            self.masterPublicKey = self._setSSH()

            self.botoPath = nodeBotoPath
            self.keyName = 'core'
            self.gceUserDataWorker = gceUserDataWithSsh

            self.nodeStorage = config.nodeStorage
            spotBids = []
            self.nonPreemptableNodeTypes = []
            self.preemptableNodeTypes = []
            for nodeTypeStr in config.nodeTypes:
                nodeBidTuple = nodeTypeStr.split(":")
                if len(nodeBidTuple) == 2:
                    #This is a preemptable node type, with a spot bid
                    self.preemptableNodeTypes.append(nodeBidTuple[0])
                    spotBids.append(nodeBidTuple[1])
                else:
                    self.nonPreemptableNodeTypes.append(nodeTypeStr)
            self.preemptableNodeShapes = [self.getNodeShape(nodeType=nodeType, preemptable=True) for nodeType in self.preemptableNodeTypes]
            self.nonPreemptableNodeShapes = [self.getNodeShape(nodeType=nodeType, preemptable=False) for nodeType in self.nonPreemptableNodeTypes]

            self.nodeShapes = self.nonPreemptableNodeShapes + self.preemptableNodeShapes
            self.nodeTypes = self.nonPreemptableNodeTypes + self.preemptableNodeTypes
            self.spotBids = dict(zip(self.preemptableNodeTypes, spotBids))
        else:
            self.clusterName = None
            self.instanceMetaData = None
            self.leaderIP = None
            self.keyName = None
            self.tags = None
            self.masterPublicKey = None
            self.nodeStorage = None
            self.gceUserDataWorker = gceUserData
            self.botoPath = None

            # TODO: This is not necessary for ssh and rsync.
            if os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
                self.googleJson = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
            else:
                raise RuntimeError('GOOGLE_APPLICATION_CREDENTIALS not set.')
            try:
                with open(self.googleJson) as jsonFile:
                    self.googleConnectionParams = json.loads(jsonFile.read())
            except:
                 raise RuntimeError('GCEProvisioner: Could not parse the Google service account json file %s' % self.googleJson)

            self.projectId = self.googleConnectionParams['project_id']
            self.clientEmail = self.googleConnectionParams['client_email']

        self.subnetID = None



    def launchCluster(self, leaderNodeType, leaderSpotBid, nodeTypes, preemptableNodeTypes, keyName,
            clusterName, numWorkers=0, numPreemptableWorkers=0, spotBids=None, userTags=None, zone=None,
            vpcSubnet=None, leaderStorage=50, nodeStorage=50,
            botoPath=None):
        if self.config is None:
            self.nodeStorage = nodeStorage
        if userTags is None:
            userTags = {}
        self.zone = zone
        self.clusterName = clusterName
        self.botoPath = botoPath
        self.keyName = keyName

        # GCE doesn't have a dictionary tags field. The tags field is just a string list.
        # Therefore, umping tags into the description.
        tags = {'Owner': keyName}
        tags.update(userTags)
        self.tags = json.dumps(tags)

        # TODO
        # - security group: just for a cluster identifier?
        # - Error thrown if cluster exists. Add an explicit check for an existing cluster? Racey though.

        leaderData = dict(role='leader',
                          dockerImage=applianceSelf(),
                          entrypoint='mesos-master',
                          dockerArgs=leaderDockerArgs.format(name=clusterName))
        userData = gceUserData.format(**leaderData)
        metadata = {'items': [{'key': 'user-data', 'value': userData}]}

        imageType = 'coreos-stable'
        sa_scopes = [{'scopes': ['compute', 'storage-full']}]

        driver = self._getDriver()
        if False:
            # TODO: remove this - for testing only
            leader = self._getLeader(clusterName, zone=zone)
        elif not leaderSpotBid:
            logger.info('Launching non-preemptable leader')
            disk = {}
            disk['initializeParams'] = {
                'sourceImage': bytes('https://www.googleapis.com/compute/v1/projects/coreos-cloud/global/images/coreos-stable-1576-4-0-v20171206'),
                'diskSizeGb' : leaderStorage }
            disk.update({'boot': True,
                 #'type': 'bytes('zones/us-central1-a/diskTypes/local-ssd'), #'PERSISTANT'
                 #'mode': 'READ_WRITE',
                 #'deviceName': clusterName,
                 'autoDelete': True })
            leader = driver.create_node(clusterName, leaderNodeType, imageType,
                                    location=zone,
                                    ex_service_accounts=sa_scopes,
                                    ex_metadata=metadata,
                                    ex_subnetwork=vpcSubnet,
                                    ex_disks_gce_struct = [disk],
                                    description=self.tags)
        else:
            logger.info('Launching preemptable leader')
            # force generator to evaluate
            list(create_spot_instances(ec2=ctx.ec2,
                                       price=leaderSpotBid,
                                       image_id=self._discoverAMI(ctx),
                                       spec=kwargs,
                                       num_instances=1))

        logger.info('... toil_leader is running')

        # if we are running launch cluster we need to save this data as it won't be generated
        # from the metadata. This data is needed to launch worker nodes.
        self.leaderIP = leader.private_ips[0]
        if spotBids:
            self.spotBids = dict(zip(preemptableNodeTypes, spotBids))

        #TODO: get subnetID
        #self.subnetID = leader.subnet_id

        self._copySshKeys(leader.public_ips[0], keyName)
        self._waitForNode(leader.public_ips[0], 'toil_leader')

        if self.botoPath:
            self._rsyncNode(leader.public_ips[0], [self.botoPath, ':' + nodeBotoPath],
                            applianceName='toil_leader')

        # assuming that if the leader was launched without a spotbid then all workers
        # will be non-preemptable
        workersCreated = 0
        for nodeType, workers in zip(nodeTypes, numWorkers):
            workersCreated += self.addNodes(nodeType=nodeType, numNodes=workers, preemptable=False)
        for nodeType, workers in zip(preemptableNodeTypes, numPreemptableWorkers):
            workersCreated += self.addNodes(nodeType=nodeType, numNodes=workers, preemptable=True)
        logger.info('Added %d workers', workersCreated)

        return leader

    def getNodeShape(self, nodeType, preemptable=False):
        sizes = self._getDriver().list_sizes(location=self.zone)
        sizes = [x for x in sizes if x.name == nodeType]
        assert len(sizes) == 1
        instanceType = sizes[0]

        disk = 0 #instanceType.disks * instanceType.disk_capacity * 2 ** 30
        if disk == 0:
            # This is an EBS-backed instance. We will use the root
            # volume, so add the amount of EBS storage requested for
            # the root volume
            disk = self.nodeStorage * 2 ** 30

        # Ram is in M.
        #Underestimate memory by 100M to prevent autoscaler from disagreeing with
        #mesos about whether a job can run on a particular node type
        memory = (instanceType.ram/1000 - 0.1) * 2** 30
        return Shape(wallTime=60 * 60,
                     memory=memory,
                     cores=instanceType.extra['guestCpus'],
                     disk=disk,
                     preemptable=preemptable)

    @staticmethod
    def retryPredicate(e):
        return GCEProvisioner._throttlePredicate(e)

    @staticmethod
    def _throttlePredicate(e):
        return False

    def destroyCluster(self, clusterName, zone=None):
        #TODO: anything like this with Google?
        #spotIDs = self._getSpotRequestIDs(ctx, clusterName)
        #if spotIDs:
        #    ctx.ec2.cancel_spot_instance_requests(request_ids=spotIDs)

        if zone is not None:
            self.zone = zone
        instancesToTerminate = self._getNodesInCluster(clusterName)
        if instancesToTerminate:
            self._terminateInstances(instances=instancesToTerminate)

    def sshLeader(self, clusterName, args=None, zone=None, **kwargs):
        leader = self._getLeader(clusterName, zone=zone)
        logger.info('SSH ready')
        kwargs['tty'] = sys.stdin.isatty()
        command = args if args else ['bash']
        self._sshAppliance(leader.public_ips[0], *command, **kwargs)

    def rsyncLeader(self, clusterName, args, zone=None, **kwargs):
        leader = self._getLeader(clusterName, zone=zone)
        self._rsyncNode(leader.public_ips[0], args, **kwargs)

    def remainingBillingInterval(self, node):
        #TODO - does this exist in GCE?
        return 1 #awsRemainingBillingInterval(node)

    def terminateNodes(self, nodes):
        nodeNames = [n.name for n in nodes]
        instances = self._getNodesInCluster(self.clusterName)
        instancesToKill = [i for i in instances if i.name in nodeNames]
        self._terminateInstances(instancesToKill)

    def addNodes(self, nodeType, numNodes, preemptable):
        # If keys are rsynced, then the mesos-slave needs to be started after the keys have been
        # transferred. The waitForKey.sh script loops on the new VM until it finds the keyPath file, then it starts the
        # mesos-slave. If there are multiple keys to be transferred, then the last one to be transferred must be
        # set to keyPath.
        keyPath = ''
        entryPoint = 'mesos-slave'
        botoExists = os.path.exists(self.botoPath)
        if botoExists:
            entryPoint = "waitForKey.sh"
            keyPath = nodeBotoPath
        elif self.config and self.config.sseKey:
            entryPoint = "waitForKey.sh"
            keyPath = self.config.sseKey

        workerData = dict(role='worker',
                          dockerImage=applianceSelf(),
                          entrypoint=entryPoint,
                          sshKey=self.masterPublicKey,
                          dockerArgs=workerDockerArgs.format(ip=self.leaderIP, preemptable=preemptable, keyPath=keyPath))

        #kwargs["subnet_id"] = self.subnetID if self.subnetID else self._getClusterInstance(self.instanceMetaData).subnet_id

        userData = self.gceUserDataWorker.format(**workerData)
        metadata = {'items': [{'key': 'user-data', 'value': userData}]}

        imageType = 'coreos-stable'
        sa_scopes = [{'scopes': ['compute', 'storage-full']}]

        # TODO:
        #    - node/volume naming: clusterName-???, How to increment across restarts?
        #    - bug in gce.py for ex_create_multiple_nodes (erroneously, doesn't allow image and disk to specified)
        driver = self._getDriver()
        if not preemptable:
            logger.info('Launching %s non-preemptable nodes', numNodes)
            disk = {}
            disk['initializeParams'] = {
                'sourceImage': bytes('https://www.googleapis.com/compute/v1/projects/coreos-cloud/global/images/coreos-stable-1576-4-0-v20171206'),
                'diskSizeGb' : self.nodeStorage }
            disk.update({'boot': True,
                 #'type': 'bytes('zones/us-central1-a/diskTypes/local-ssd'), #'PERSISTANT'
            #     'mode': 'READ_WRITE',
            #     'deviceName': clusterName,
                 'autoDelete': True })
            #instancesLaunched = driver.ex_create_multiple_nodes(
            instancesLaunched = self.ex_create_multiple_nodes(
                                    self.clusterName, nodeType, imageType, numNodes,
                                    location=self.zone,
                                    ex_service_accounts=sa_scopes,
                                    ex_metadata=metadata,
                                    ex_disks_gce_struct = [disk],
                                    description=self.tags
                                    )
        else:
            logger.info('Launching %s preemptable nodes', numNodes)
            kwargs['placement'] = getSpotZone(self.spotBids[nodeType], instanceType.name, self.ctx)
            # force generator to evaluate
            instancesLaunched = list(create_spot_instances(ec2=self.ctx.ec2,
                                                           price=self.spotBids[nodeType],
                                                           image_id=self._discoverAMI(self.ctx),
                                                           tags={'clusterName': self.clusterName},
                                                           spec=kwargs,
                                                           num_instances=numNodes,
                                                           tentative=True))
            # flatten the list
            instancesLaunched = [item for sublist in instancesLaunched for item in sublist]

        for instance in instancesLaunched:
            self._copySshKeys(instance.public_ips[0], self.keyName)
            if self.config and self.config.sseKey or botoExists:
                self._waitForNode(instance.public_ips[0], 'toil_worker')
                if self.config and self.config.sseKey:
                    self._rsyncNode(instance.public_ips[0], [self.config.sseKey, ':' + self.config.sseKey],
                                    applianceName='toil_worker')
                if botoExists:
                    self._rsyncNode(instance.public_ips[0], [self.botoPath, ':' + nodeBotoPath],
                                    applianceName='toil_worker')
        logger.info('Launched %s new instance(s)', numNodes)
        return len(instancesLaunched)

    def getProvisionedWorkers(self, nodeType, preemptable):
        entireCluster = self._getNodesInCluster(clusterName=self.clusterName, nodeType=nodeType)
        logger.debug('All nodes in cluster: %s', entireCluster)
        workerInstances = [i for i in entireCluster if i.private_ips[0] != self.leaderIP]
        logger.debug('All workers found in cluster: %s', workerInstances)
        # TODO: get spot workers
        #workerInstances = [i for i in workerInstances if preemptable != (i.spot_instance_request_id is None)]
        #logger.debug('%spreemptable workers found in cluster: %s', 'non-' if not preemptable else '', workerInstances)
        return [Node(publicIP=i.public_ips[0], privateIP=i.private_ips[0],
                     name=i.name, launchTime=i.created_at, nodeType=i.size,
                     preemptable=preemptable)
                for i in workerInstances]

    def _getLeader(self, clusterName, zone=None):
        if zone is not None:
            self.zone = zone
        instances = self._getNodesInCluster(clusterName)

        instances.sort(key=lambda x: x.created_at)
        try:
            leader = instances[0]  # assume leader was launched first
        except IndexError:
            raise NoSuchClusterException(clusterName)
        return leader

    def _getNodesInCluster(self, clusterName, nodeType=None):
        allInstances = self._getDriver().list_nodes(ex_zone=self.zone)
        instances = [instance for instance in allInstances if instance.name.startswith(clusterName)]
        if nodeType:
            instances = [instance for instance in instances if instance.size == nodeType]
        return instances

    def _getDriver(self):
        driverCls = get_driver(Provider.GCE)
        return driverCls(self.clientEmail,
                         self.googleJson,
                         project=self.projectId,
                         datacenter=self.zone)

    @classmethod
    def _copySshKeys(cls, instanceIP, keyName):
        """ Copy authorized_keys file to the core user from the keyName user."""
        if keyName == 'core':
            return

        # Make sure that keys are there.
        cls._waitForSSHKeys(instanceIP, keyName=keyName)

        # TODO: Check if there is another way to ssh to a GCE instance with Google credentials

        # copy keys to core user so that the ssh calls will work
        # - normal mechanism failed unless public key was in the google-ssh format
        # - even so, the key wasn't copied correctly to the core account
        keyFile = '/home/%s/.ssh/authorized_keys' % keyName
        cls._sshInstance(instanceIP, '/usr/bin/sudo', '/usr/bin/cp', keyFile, '/home/core/.ssh', user=keyName)
        cls._sshInstance(instanceIP, '/usr/bin/sudo', '/usr/bin/chown', 'core', '/home/core/.ssh/authorized_keys', user=keyName)

    def _waitForNode(self, instanceIP, role):
        # wait here so docker commands can be used reliably afterwards
        # TODO: make this more robust, e.g. If applicance doesn't exist, then this waits forever.
        self._waitForSSHKeys(instanceIP, keyName=self.keyName)
        self._waitForDockerDaemon(instanceIP, keyName=self.keyName)
        self._waitForAppliance(instanceIP, role=role, keyName=self.keyName)

    @classmethod
    def _coreSSH(cls, nodeIP, *args, **kwargs):
        """
        If strict=False, strict host key checking will be temporarily disabled.
        This is provided as a convenience for internal/automated functions and
        ought to be set to True whenever feasible, or whenever the user is directly
        interacting with a resource (e.g. rsync-cluster or ssh-cluster). Assumed
        to be False by default.

        kwargs: input, tty, appliance, collectStdout, sshOptions, strict
        """
        commandTokens = ['ssh', '-t']
        strict = kwargs.pop('strict', False)
        if not strict:
            kwargs['sshOptions'] = ['-oUserKnownHostsFile=/dev/null', '-oStrictHostKeyChecking=no'] + kwargs.get('sshOptions', [])
        sshOptions = kwargs.pop('sshOptions', None)
        #Forward port 3000 for grafana dashboard
        commandTokens.extend(['-L', '3000:localhost:3000', '-L', '9090:localhost:9090'])
        if sshOptions:
            # add specified options to ssh command
            assert isinstance(sshOptions, list)
            commandTokens.extend(sshOptions)
        # specify host
        user = kwargs.pop('user', 'core')   # CHANGED: Is this needed?
        commandTokens.append('%s@%s' % (user,str(nodeIP)))
        appliance = kwargs.pop('appliance', None)
        if appliance:
            # run the args in the appliance
            tty = kwargs.pop('tty', None)
            ttyFlag = '-t' if tty else ''
            commandTokens += ['docker', 'exec', '-i', ttyFlag, 'toil_leader']
        inputString = kwargs.pop('input', None)
        if inputString is not None:
            kwargs['stdin'] = subprocess.PIPE
        collectStdout = kwargs.pop('collectStdout', None)
        if collectStdout:
            kwargs['stdout'] = subprocess.PIPE
        logger.debug('Node %s: %s', nodeIP, ' '.join(args))
        args = list(map(pipes.quote, args))
        commandTokens += args
        logger.debug('Full command %s', ' '.join(commandTokens))
        popen = subprocess.Popen(commandTokens, **kwargs)
        stdout, stderr = popen.communicate(input=inputString)
        # at this point the process has already exited, no need for a timeout
        resultValue = popen.wait()
        if resultValue != 0:
            raise RuntimeError('Executing the command "%s" on the appliance returned a non-zero '
                               'exit code %s with stdout %s and stderr %s' % (' '.join(args), resultValue, stdout, stderr))
        assert stderr is None
        return stdout


    def _terminateInstances(self, instances):
        def worker(driver, instance):
            logger.info('Terminating instance: %s', instance.name)
            driver.destroy_node(instance)

        driver = self._getDriver()
        threads = []
        for instance in instances:
            t = threading.Thread(target=worker, args=(driver,instance))
            threads.append(t)
            t.start()

        logger.info('... Waiting for instance(s) to shut down...')
        for t in threads:
            t.join()

    DEFAULT_TASK_COMPLETION_TIMEOUT = 180
    def ex_create_multiple_nodes(
            self, base_name, size, image, number, location=None,
            ex_network='default', ex_subnetwork=None, ex_tags=None,
            ex_metadata=None, ignore_errors=True, use_existing_disk=True,
            poll_interval=2, external_ip='ephemeral',
            ex_disk_type='pd-standard', ex_disk_auto_delete=True,
            ex_service_accounts=None, timeout=DEFAULT_TASK_COMPLETION_TIMEOUT,
            description=None, ex_can_ip_forward=None, ex_disks_gce_struct=None,
            ex_nic_gce_struct=None, ex_on_host_maintenance=None,
            ex_automatic_restart=None, ex_image_family=None,
            ex_preemptible=None):
        """
         Monkey patch to gce.py in libcloud to allow disk and images to be specified.
        """
        # if image and ex_disks_gce_struct:
        #    raise ValueError("Cannot specify both 'image' and "
        #                     "'ex_disks_gce_struct'.")

        driver = self._getDriver()
        if image and ex_image_family:
            raise ValueError("Cannot specify both 'image' and "
                             "'ex_image_family'")

        location = location or driver.zone
        if not hasattr(location, 'name'):
            location = driver.ex_get_zone(location)
        if not hasattr(size, 'name'):
            size = driver.ex_get_size(size, location)
        if not hasattr(ex_network, 'name'):
            ex_network = driver.ex_get_network(ex_network)
        if ex_subnetwork and not hasattr(ex_subnetwork, 'name'):
            ex_subnetwork = \
                driver.ex_get_subnetwork(ex_subnetwork,
                                       region=driver._get_region_from_zone(
                                           location))
        if ex_image_family:
            image = driver.ex_get_image_from_family(ex_image_family)
        if image and not hasattr(image, 'name'):
            image = driver.ex_get_image(image)
        if not hasattr(ex_disk_type, 'name'):
            ex_disk_type = driver.ex_get_disktype(ex_disk_type, zone=location)

        node_attrs = {'size': size,
                      'image': image,
                      'location': location,
                      'network': ex_network,
                      'subnetwork': ex_subnetwork,
                      'tags': ex_tags,
                      'metadata': ex_metadata,
                      'ignore_errors': ignore_errors,
                      'use_existing_disk': use_existing_disk,
                      'external_ip': external_ip,
                      'ex_disk_type': ex_disk_type,
                      'ex_disk_auto_delete': ex_disk_auto_delete,
                      'ex_service_accounts': ex_service_accounts,
                      'description': description,
                      'ex_can_ip_forward': ex_can_ip_forward,
                      'ex_disks_gce_struct': ex_disks_gce_struct,
                      'ex_nic_gce_struct': ex_nic_gce_struct,
                      'ex_on_host_maintenance': ex_on_host_maintenance,
                      'ex_automatic_restart': ex_automatic_restart,
                      'ex_preemptible': ex_preemptible}
        # List for holding the status information for disk/node creation.
        status_list = []

        for i in range(number):
            name = '%s-%03d' % (base_name, i)
            status = {'name': name, 'node_response': None, 'node': None}
            status_list.append(status)

        start_time = time.time()
        complete = False
        while not complete:
            if (time.time() - start_time >= timeout):
                raise Exception("Timeout (%s sec) while waiting for multiple "
                                "instances")
            complete = True
            time.sleep(poll_interval)
            for status in status_list:
                # Create the node or check status if already in progress.
                if not status['node']:
                    if not status['node_response']:
                        driver._multi_create_node(status, node_attrs)
                    else:
                        driver._multi_check_node(status, node_attrs)
                # If any of the nodes have not been created (or failed) we are
                # not done yet.
                if not status['node']:
                    complete = False

        # Return list of nodes
        node_list = []
        for status in status_list:
            node_list.append(status['node'])
        return node_list


    ## UNCHANGED CLASSES

    @classmethod
    def _waitForSSHKeys(cls, instanceIP, keyName='core'):
        # the propagation of public ssh keys vs. opening the SSH port is racey, so this method blocks until
        # the keys are propagated and the instance can be SSH into
        while True:
            try:
                logger.info('Attempting to establish SSH connection...')
                cls._sshInstance(instanceIP, 'ps', sshOptions=['-oBatchMode=yes'], user=keyName)
            except RuntimeError:
                logger.info('Connection rejected, waiting for public SSH key to be propagated. Trying again in 10s.')
                time.sleep(10)
            else:
                logger.info('...SSH connection established.')
                # ssh succeeded
                return

    @classmethod
    def _waitForDockerDaemon(cls, ip_address, keyName='core'):
        logger.info('Waiting for docker on %s to start...', ip_address)
        while True:
            output = cls._sshInstance(ip_address, '/usr/bin/ps', 'aux', sshOptions=['-oBatchMode=yes'], user=keyName)
            time.sleep(5)
            if 'dockerd' in output:
                # docker daemon has started
                break
            else:
                logger.info('... Still waiting...')
        logger.info('Docker daemon running')

    @classmethod
    def _waitForAppliance(cls, ip_address, role, keyName='core'):
        logger.info('Waiting for %s Toil appliance to start...', role)
        while True:
            output = cls._sshInstance(ip_address, '/usr/bin/docker', 'ps', sshOptions=['-oBatchMode=yes'], user=keyName)
            if role in output:
                logger.info('...Toil appliance started')
                break
            else:
                logger.info('...Still waiting, trying again in 10sec...')
                time.sleep(10)

    @classmethod
    def _rsyncNode(cls, ip, args, applianceName='toil_leader', **kwargs):
        remoteRsync = "docker exec -i %s rsync" % applianceName  # Access rsync inside appliance
        parsedArgs = []
        sshCommand = "ssh"
        strict = kwargs.pop('strict', False)
        if not strict:
            sshCommand = "ssh -oUserKnownHostsFile=/dev/null -oStrictHostKeyChecking=no"
        hostInserted = False
        # Insert remote host address
        for i in args:
            if i.startswith(":") and not hostInserted:
                i = ("core@%s" % ip) + i
                hostInserted = True
            elif i.startswith(":") and hostInserted:
                raise ValueError("Cannot rsync between two remote hosts")
            parsedArgs.append(i)
        if not hostInserted:
            raise ValueError("No remote host found in argument list")
        command = ['rsync', '-e', sshCommand, '--rsync-path', remoteRsync]
        logger.debug("Running %r.", command + parsedArgs)

        return subprocess.check_call(command + parsedArgs)

    def _setSSH(self):
        if not os.path.exists('/root/.sshSuccess'):
            subprocess.check_call(['ssh-keygen', '-f', '/root/.ssh/id_rsa', '-t', 'rsa', '-N', ''])
            with open('/root/.sshSuccess', 'w') as f:
                f.write('written here because of restrictive permissions on .ssh dir')
        os.chmod('/root/.ssh', 0o700)
        subprocess.check_call(['bash', '-c', 'eval $(ssh-agent) && ssh-add -k'])
        with open('/root/.ssh/id_rsa.pub') as f:
            masterPublicKey = f.read()
        masterPublicKey = masterPublicKey.split(' ')[1]  # take 'body' of key
        # confirm it really is an RSA public key
        assert masterPublicKey.startswith('AAAAB3NzaC1yc2E'), masterPublicKey
        return masterPublicKey