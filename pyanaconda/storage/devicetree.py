# devicetree.py
# Device management for anaconda's storage configuration module.
#
# Copyright (C) 2009  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
# Red Hat Author(s): Dave Lehman <dlehman@redhat.com>
#

import os
import stat
import block
import re
import shutil

from errors import *
from devices import *
from deviceaction import *
from partitioning import shouldClear
from pykickstart.constants import *
import formats
import devicelibs.mdraid
import devicelibs.dm
import devicelibs.lvm
import devicelibs.mpath
import devicelibs.loop
from udev import *
from pyanaconda import iutil
from pyanaconda import tsort
from pyanaconda.anaconda_log import log_method_call, log_method_return
import parted
import _ped

import gettext
_ = lambda x: gettext.ldgettext("anaconda", x)

import logging
log = logging.getLogger("storage")

def getLUKSPassphrase(intf, device, globalPassphrase):
    """ Obtain a passphrase for a LUKS encrypted block device.

        The format's mapping name must already be set and the backing
        device must already be set up before calling this function.

        If successful, this function leaves the device mapped.

        Return value is a two-tuple: (passphrase, isglobal)

        passphrase is the passphrase string, if obtained
        isglobal is a boolean indicating whether the passphrase is global

        Either or both can be None, depending on the outcome.
    """
    if device.format.type != "luks":
        # this function only works on luks devices
        raise ValueError("not a luks device")

    if not device.status:
        # the device should have already been set up
        raise RuntimeError("device is not set up")

    if device.format.status:
        # the device is already mapped
        raise RuntimeError("device is already mapped")

    if not device.format.configured and globalPassphrase:
        # try the given passphrase first
        device.format.passphrase =  globalPassphrase
    
        try:
            device.format.setup()
        except CryptoError as e:
            device.format.passphrase = None
        else:
            # we've opened the device so we're done.
            return (globalPassphrase, False)

    if not intf:
        return (None, None)
    
    buttons = [_("Back"), _("Continue")]
    passphrase_incorrect = False
    while True:
        if passphrase_incorrect:
            # TODO: add a flag to passphraseEntryWindow to say the last
            #       passphrase was incorrect so try again
            passphrase_incorrect = False
        (passphrase, isglobal) = intf.passphraseEntryWindow(device.name)
        if not passphrase:
            rc = intf.messageWindow(_("Confirm"),
                                    _("Are you sure you want to skip "
                                      "entering a passphrase for device "
                                      "%s?\n\n"
                                      "If you skip this step the "
                                      "device's contents will not "
                                      "be available during "
                                      "installation.") % device.name,
                                    type = "custom",
                                    default = 0,
                                    custom_buttons = buttons)
            if rc == 0:
                continue
            else:
                passphrase = None
                isglobal = None
                log.info("skipping passphrase for %s" % (device.name,))
                break

        device.format.passphrase = passphrase

        try:
            device.format.setup()
        except CryptoError as e:
            device.format.passphrase = None
            passphrase_incorrect = True
        else:
            # we've opened the device so we're done.
            break

    return (passphrase, isglobal)


class DeviceTree(object):
    """ A quasi-tree that represents the devices in the system.

        The tree contains a list of device instances, which does not
        necessarily reflect the actual state of the system's devices.
        DeviceActions are used to perform modifications to the tree,
        except when initially populating the tree.

        DeviceAction instances are registered, possibly causing the
        addition or removal of Device instances to/from the tree. The
        DeviceActions are all reversible up to the time their execute
        method has been called.

        Only one action of any given type/object pair should exist for
        any given device at any given time.

        DeviceAction instances can only be registered for leaf devices,
        except for resize actions.
    """

    def __init__(self, intf=None, conf=None, passphrase=None, luksDict=None,
                 iscsi=None, dasd=None):
        # internal data members
        self._devices = []
        self._actions = []

        # indicates whether or not the tree has been fully populated
        self.populated = False

        self.intf = intf
        self.exclusiveDisks = getattr(conf, "exclusiveDisks", [])
        self.clearPartType = getattr(conf, "clearPartType", CLEARPART_TYPE_NONE)
        self.clearPartDisks = getattr(conf, "clearPartDisks", [])
        self.zeroMbr = getattr(conf, "zeroMbr", False)
        self.reinitializeDisks = getattr(conf, "reinitializeDisks", False)
        self.iscsi = iscsi
        self.dasd = dasd

        self.diskImages = {}
        images = getattr(conf, "diskImages", {})
        if images:
            # this will overwrite self.exclusiveDisks
            self.setDiskImages(images)

        # protected device specs as provided by the user
        self.protectedDevSpecs = getattr(conf, "protectedDevSpecs", [])

        # names of protected devices at the time of tree population
        self.protectedDevNames = []

        self.unusedRaidMembers = []

        self.__multipaths = {}
        self.__multipathConfigWriter = devicelibs.mpath.MultipathConfigWriter()

        self.__passphrase = passphrase
        self.__luksDevs = {}
        if luksDict and isinstance(luksDict, dict):
            self.__luksDevs = luksDict
        self._ignoredDisks = []
        for disk in getattr(conf, "ignoredDisks", []):
            self.addIgnoredDisk(disk)
        devicelibs.lvm.lvm_cc_resetFilter()

        self._cleanup = False

    def setDiskImages(self, images):
        """ Set the disk images and reflect them in exclusiveDisks. """
        self.diskImages = images
        # disk image files are automatically exclusive
        self.exclusiveDisks = self.diskImages.keys()

    def addIgnoredDisk(self, disk):
        self._ignoredDisks.append(disk)
        devicelibs.lvm.lvm_cc_addFilterRejectRegexp(disk)

    def pruneActions(self):
        """ Remove redundant/obsolete actions from the action list. """
        for action in reversed(self._actions[:]):
            if action not in self._actions:
                log.debug("action '%s' already pruned" % action)
                continue

            log.debug("checking for actions obsoleted by '%s'" % action)
            for obsolete in self._actions[:]:
                log.debug(" checking action '%s'" % obsolete)
                if action.obsoletes(obsolete):
                    log.debug("  removing obsolete action '%s'" % obsolete)
                    self._actions.remove(obsolete)

    def sortActions(self):
        """ Sort actions based on dependencies. """
        if not self._actions:
            return

        edges = []

        # collect all ordering requirements for the actions
        for action in self._actions:
            action_idx = self._actions.index(action)
            children = []
            for _action in self._actions:
                if _action == action:
                    continue

                # create edges based on both action type and dependencies.
                if action.type > _action.type or _action.requires(action):
                    children.append(_action)

            for child in children:
                child_idx = self._actions.index(child)
                edges.append((action_idx, child_idx))

        # create a graph reflecting the ordering information we have
        graph = tsort.create_graph(range(len(self._actions)), edges)

        # perform a topological sort based on the graph's contents
        order = tsort.tsort(graph)

        # now replace self._actions with a sorted version of the same list
        actions = []
        for idx in order:
            actions.append(self._actions[idx])
        self._actions = actions

    def processActions(self, dryRun=None):
        """ Execute all registered actions. """
        log.debug("resetting parted disks...")
        for device in self.devices:
            if device.partitioned:
                device.format.resetPartedDisk()
                if device.originalFormat.type == "disklabel" and \
                   device.originalFormat != device.format:
                    device.originalFormat.resetPartedDisk()

        # Call preCommitFixup on all devices
        mpoints = [getattr(d.format, 'mountpoint', "") for d in self.devices]
        for device in self.devices:
            device.preCommitFixup(mountpoints=mpoints)

        # Also call preCommitFixup on any devices we're going to
        # destroy (these are already removed from the tree)
        for action in self._actions:
            if isinstance(action, ActionDestroyDevice):
                action.device.preCommitFixup(mountpoints=mpoints)

        # setup actions to create any extended partitions we added
        #
        # XXX At this point there can be duplicate partition paths in the
        #     tree (eg: non-existent sda6 and previous sda6 that will become
        #     sda5 in the course of partitioning), so we access the list
        #     directly here.
        for device in self._devices:
            if isinstance(device, PartitionDevice) and \
               device.isExtended and not device.exists:
                # don't properly register the action since the device is
                # already in the tree
                self._actions.append(ActionCreateDevice(device))

        for action in self._actions:
            log.debug("action: %s" % action)

        log.debug("pruning action queue...")
        self.pruneActions()
        for action in self._actions:
            log.debug("action: %s" % action)

        log.debug("sorting actions...")
        self.sortActions()
        for action in self._actions:
            log.debug("action: %s" % action)

        for action in self._actions:
            log.info("executing action: %s" % action)
            if not dryRun:
                try:
                    action.execute(intf=self.intf)
                except DiskLabelCommitError:
                    # it's likely that a previous format destroy action
                    # triggered setup of an lvm or md device.
                    self.teardownAll()
                    action.execute(intf=self.intf)

                udev_settle()
                for device in self._devices:
                    # make sure we catch any renumbering parted does
                    if device.exists and isinstance(device, PartitionDevice):
                        device.updateName()
                        device.format.device = device.path

    def _addDevice(self, newdev):
        """ Add a device to the tree.

            Raise ValueError if the device's identifier is already
            in the list.
        """
        if newdev.path in [d.path for d in self._devices] and \
           not isinstance(newdev, NoDevice):
            raise ValueError("device is already in tree")

        # make sure this device's parent devices are in the tree already
        for parent in newdev.parents:
            if parent not in self._devices:
                raise DeviceTreeError("parent device not in tree")

        self._devices.append(newdev)
        log.debug("added %s %s (id %d) to device tree" % (newdev.type,
                                                          newdev.name,
                                                          newdev.id))

    def _removeDevice(self, dev, force=None, moddisk=True):
        """ Remove a device from the tree.

            Only leaves may be removed.
        """
        if dev not in self._devices:
            raise ValueError("Device '%s' not in tree" % dev.name)

        if not dev.isleaf and not force:
            log.debug("%s has %d kids" % (dev.name, dev.kids))
            raise ValueError("Cannot remove non-leaf device '%s'" % dev.name)

        # if this is a partition we need to remove it from the parted.Disk
        if moddisk and isinstance(dev, PartitionDevice) and \
                dev.disk is not None:
            # if this partition hasn't been allocated it could not have
            # a disk attribute
            if dev.partedPartition.type == parted.PARTITION_EXTENDED and \
                    len(dev.disk.format.logicalPartitions) > 0:
                raise ValueError("Cannot remove extended partition %s.  "
                        "Logical partitions present." % dev.name)

            dev.disk.format.removePartition(dev.partedPartition)

            # adjust all other PartitionDevice instances belonging to the
            # same disk so the device name matches the potentially altered
            # name of the parted.Partition
            for device in self._devices:
                if isinstance(device, PartitionDevice) and \
                   device.disk == dev.disk:
                    device.updateName()

        self._devices.remove(dev)
        log.debug("removed %s %s (id %d) from device tree" % (dev.type,
                                                              dev.name,
                                                              dev.id))

        for parent in dev.parents:
            # Will this cause issues with garbage collection?
            #   Do we care about garbage collection? At all?
            parent.removeChild()

    def registerAction(self, action):
        """ Register an action to be performed at a later time.

            Modifications to the Device instance are handled before we
            get here.
        """
        if not (action.isCreate and action.isDevice) and \
           action.device not in self._devices:
            raise DeviceTreeError("device is not in the tree")
        elif (action.isCreate and action.isDevice):
            # this allows multiple create actions w/o destroy in between;
            # we will clean it up before processing actions
            #raise DeviceTreeError("device is already in the tree")
            if action.device in self._devices:
                self._removeDevice(action.device)
            for d in self._devices:
                if d.path == action.device.path:
                    self._removeDevice(d)

        if action.isCreate and action.isDevice:
            self._addDevice(action.device)
        elif action.isDestroy and action.isDevice:
            self._removeDevice(action.device)
        elif action.isCreate and action.isFormat:
            if isinstance(action.device.format, formats.fs.FS) and \
               action.device.format.mountpoint in self.filesystems:
                raise DeviceTreeError("mountpoint already in use")

        log.debug("registered action: %s" % action)
        self._actions.append(action)

    def cancelAction(self, action):
        """ Cancel a registered action.

            This will unregister the action and do any required
            modifications to the device list.

            Actions all operate on a Device, so we can use the devices
            to determine dependencies.
        """
        if action.isCreate and action.isDevice:
            # remove the device from the tree
            self._removeDevice(action.device)
        elif action.isDestroy and action.isDevice:
            # add the device back into the tree
            self._addDevice(action.device)
        elif action.isFormat and \
             (action.isCreate or action.isMigrate or action.isResize):
            action.cancel()

        self._actions.remove(action)

    def findActions(self, device=None, type=None, object=None, path=None,
                    devid=None):
        """ Find all actions that match all specified parameters.

            Keyword arguments:

                device -- device to match (Device, or None to match any)
                type -- action type to match (string, or None to match any)
                object -- operand type to match (string, or None to match any)
                path -- device path to match (string, or None to match any)

        """
        if device is None and type is None and object is None and \
           path is None and devid is None:
            return self._actions[:]

        # convert the string arguments to the types used in actions
        _type = action_type_from_string(type)
        _object = action_object_from_string(object)

        actions = []
        for action in self._actions:
            if device is not None and action.device != device:
                continue

            if _type is not None and action.type != _type:
                continue

            if _object is not None and action.obj != _object:
                continue

            if path is not None and action.device.path != path:
                continue

            if devid is not None and action.device.id != devid:
                continue
                
            actions.append(action)

        return actions

    def getDependentDevices(self, dep):
        """ Return a list of devices that depend on dep.

            The list includes both direct and indirect dependents.
        """
        dependents = []

        # special handling for extended partitions since the logical
        # partitions and their deps effectively depend on the extended
        logicals = []
        if isinstance(dep, PartitionDevice) and dep.partType and \
           dep.isExtended:
            # collect all of the logicals on the same disk
            for part in self.getDevicesByInstance(PartitionDevice):
                if part.partType and part.isLogical and part.disk == dep.disk:
                    logicals.append(part)

        for device in self.devices:
            if device.dependsOn(dep):
                dependents.append(device)
            else:
                for logical in logicals:
                    if device.dependsOn(logical):
                        dependents.append(device)
                        break

        return dependents

    def isIgnored(self, info):
        """ Return True if info is a device we should ignore.

            Arguments:

                info -- a dict representing a udev db entry

            TODO:

                - filtering of SAN/FC devices
                - filtering by driver?

        """
        sysfs_path = udev_device_get_sysfs_path(info)
        name = udev_device_get_name(info)
        if not sysfs_path:
            return None

        if name in self._ignoredDisks:
            log.debug("device '%s' in ignoredDisks" % name)
            return True

        # Special handling for mdraid external metadata sets (mdraid BIOSRAID):
        # 1) The containers are intermediate devices which will never be
        # in exclusiveDisks
        # 2) Sets get added to exclusive disks with their dmraid set name by
        # the filter ui.  Note that making the ui use md names instead is not
        # possible as the md names are simpy md# and we cannot predict the #
        if udev_device_is_md(info) and \
           udev_device_get_md_level(info) == "container":
            return False

        if udev_device_get_md_container(info) and \
               udev_device_is_md(info) and \
               udev_device_get_md_name(info):
            md_name = udev_device_get_md_name(info)
            # mdadm may have appended _<digit>+ if the current hostname
            # does not match the one in the array metadata
            alt_name = re.sub("_\d+$", "", md_name)
            raw_pattern = "isw_[a-z]*_%s"
            for i in range(0, len(self.exclusiveDisks)):
                if re.match(raw_pattern % md_name, self.exclusiveDisks[i]) or \
                   re.match(raw_pattern % alt_name, self.exclusiveDisks[i]):
                    self.exclusiveDisks[i] = name
                    return False

        # never ignore mapped disk images. if you don't want to use them,
        # don't specify them in the first place
        if udev_device_is_dm_anaconda(info) or udev_device_is_dm_livecd(info):
            return False

        # Ignore loop and ram devices, we normally already skip these in
        # udev.py: enumerate_block_devices(), but we can still end up trying
        # to add them to the tree when they are slaves of other devices, this
        # happens for example with the livecd
        if name.startswith("ram"):
            return True

        if name.startswith("loop"):
            # ignore loop devices unless they're backed by a file
            return (not devicelibs.loop.get_backing_file(name))

        # We want exclusiveDisks to operate on anything that could be
        # considered a directly usable disk, ie: fwraid array, mpath, or disk.
        #
        # Unfortunately, since so many things are represented as disks by
        # udev/sysfs, we have to define what is a disk in terms of what is
        # not a disk.
        if udev_device_is_disk(info) and \
           not udev_device_is_dm_partition(info) and \
           not udev_device_is_dm_lvm(info) and \
           not udev_device_is_dm_crypt(info) and \
           not (udev_device_is_md(info) and
                not udev_device_get_md_container(info)):
            if self.exclusiveDisks and name not in self.exclusiveDisks:
                log.debug("device '%s' not in exclusiveDisks" % name)
                self.addIgnoredDisk(name)
                return True

        # FIXME: check for virtual devices whose slaves are on the ignore list

    def addUdevLVDevice(self, info):
        name = udev_device_get_name(info)
        log_method_call(self, name=name)
        uuid = udev_device_get_uuid(info)
        sysfs_path = udev_device_get_sysfs_path(info)

        # initiate detection of all PVs and hope that it leads to us having
        # the VG and LVs in the tree
        for pv_name in os.listdir("/sys" + sysfs_path + "/slaves"):
            link = os.readlink("/sys" + sysfs_path + "/slaves/" + pv_name)
            pv_sysfs_path = os.path.normpath(sysfs_path + '/slaves/' + link)
            pv_info = udev_get_block_device(pv_sysfs_path)
            self.addUdevDevice(pv_info)

        vg_name = udev_device_get_vg_name(info)
        device = self.getDeviceByName(vg_name)
        if not device:
            log.error("failed to find vg '%s' after scanning pvs" % vg_name)
        return device

    def addUdevDMDevice(self, info):
        name = udev_device_get_name(info)
        log_method_call(self, name=name)
        uuid = udev_device_get_uuid(info)
        sysfs_path = udev_device_get_sysfs_path(info)
        device = None

        for dmdev in self.devices:
            if not isinstance(dmdev, DMDevice):
                continue

            try:
                # there is a device in the tree already with the same
                # major/minor as this one but with a different name
                # XXX this is kind of racy
                if dmdev.getDMNode() == os.path.basename(sysfs_path):
                    # XXX should we take the name already in use?
                    device = dmdev
                    break
            except DMError:
                # This is a little lame, but the VG device is a DMDevice
                # and it won't have a dm node. At any rate, this is not
                # important enough to crash the install.
                log.debug("failed to find dm node for %s" % dmdev.name)
                continue

        cleanup_luks = udev_device_is_dm_luks(info) and self._cleanup
        slave_dev = None
        slave_info = None
        if device is None:
            # we couldn't find it, so create it
            # first, get a list of the slave devs and look them up
            dir = os.path.normpath("/sys/%s/slaves" % sysfs_path)
            slave_names = os.listdir(dir)
            for slave_name in slave_names:
                # if it's a dm-X name, resolve it to a map name first
                if slave_name.startswith("dm-"):
                    dev_name = dm.name_from_dm_node(slave_name)
                else:
                    dev_name = slave_name
                slave_dev = self.getDeviceByName(dev_name)
                path = os.path.normpath("%s/%s" % (dir, slave_name))
                new_info = udev_get_block_device(os.path.realpath(path)[4:])
                if not slave_dev:
                    # we haven't scanned the slave yet, so do it now
                    if new_info:
                        self.addUdevDevice(new_info)
                        slave_dev = self.getDeviceByName(dev_name)
                        if slave_dev is None:
                            # if the current slave is still not in
                            # the tree, something has gone wrong
                            log.error("failure scanning device %s: could not add slave %s" % (name, dev_name))
                            return

                if cleanup_luks:
                    slave_info = new_info

            # try to get the device again now that we've got all the slaves
            device = self.getDeviceByName(name)

            if device is None and udev_device_is_dm_partition(info):
                diskname = udev_device_get_dm_partition_disk(info)
                disk = self.getDeviceByName(diskname)
                return self.addUdevPartitionDevice(info, disk=disk)

            # if this is a luks device whose map name is not what we expect,
            # fix up the map name and see if that sorts us out
            if device is None and cleanup_luks and slave_info and slave_dev:
                slave_dev.format.mapName = name
                self.handleUdevLUKSFormat(slave_info, slave_dev)

                # try once more to get the device
                device = self.getDeviceByName(name)

            # create a device for the livecd OS image(s)
            if device is None and udev_device_is_dm_livecd(info):
                device = DMDevice(name, dmUuid=info.get('DM_UUID'),
                                  sysfsPath=sysfs_path, exists=True,
                                  parents=[slave_dev])
                device.protected = True
                device.controllable = False
                self._addDevice(device)

            # if we get here, we found all of the slave devices and
            # something must be wrong -- if all of the slaves are in
            # the tree, this device should be as well
            if device is None:
                devicelibs.lvm.lvm_cc_addFilterRejectRegexp(name)
                log.warning("ignoring dm device %s" % name)

        return device

    def addUdevMDDevice(self, info):
        name = udev_device_get_name(info)
        log_method_call(self, name=name)
        uuid = udev_device_get_uuid(info)
        sysfs_path = udev_device_get_sysfs_path(info)
        device = None

        slaves = []
        dir = os.path.normpath("/sys/%s/slaves" % sysfs_path)
        slave_names = os.listdir(dir)
        for slave_name in slave_names:
            # if it's a dm-X name, resolve it to a map name
            if slave_name.startswith("dm-"):
                dev_name = dm.name_from_dm_node(slave_name)
            else:
                dev_name = slave_name
            slave_dev = self.getDeviceByName(dev_name)
            if slave_dev:
                slaves.append(slave_dev)
            else:
                # we haven't scanned the slave yet, so do it now
                path = os.path.normpath("%s/%s" % (dir, slave_name))
                new_info = udev_get_block_device(os.path.realpath(path)[4:])
                if new_info:
                    self.addUdevDevice(new_info)
                    if self.getDeviceByName(dev_name) is None:
                        # if the current slave is still not in
                        # the tree, something has gone wrong
                        log.error("failure scanning device %s: could not add slave %s" % (name, dev_name))
                        return

        # try to get the device again now that we've got all the slaves
        device = self.getDeviceByName(name)

        if device is None:
            device = self.getDeviceByUuid(info.get("MD_UUID"))
            if device:
                raise DeviceTreeError("MD RAID device %s already in "
                                      "devicetree as %s" % (name, device.name))

        # if we get here, we found all of the slave devices and
        # something must be wrong -- if all of the slaves we in
        # the tree, this device should be as well
        if device is None:
            raise DeviceTreeError("MD RAID device %s not in devicetree after "
                                  "scanning all slaves" % name)
        return device

    def addUdevPartitionDevice(self, info, disk=None):
        name = udev_device_get_name(info)
        log_method_call(self, name=name)
        uuid = udev_device_get_uuid(info)
        sysfs_path = udev_device_get_sysfs_path(info)
        device = None

        if disk is None:
            disk_name = os.path.basename(os.path.dirname(sysfs_path))
            disk_name = disk_name.replace('!','/')
            disk = self.getDeviceByName(disk_name)

        if disk is None:
            # create a device instance for the disk
            new_info = udev_get_block_device(os.path.dirname(sysfs_path))
            if new_info:
                self.addUdevDevice(new_info)
                disk = self.getDeviceByName(disk_name)

            if disk is None:
                # if the current device is still not in
                # the tree, something has gone wrong
                log.error("failure scanning device %s" % disk_name)
                devicelibs.lvm.lvm_cc_addFilterRejectRegexp(name)
                return

        # Check that the disk has partitions. If it does not, we must have
        # reinitialized the disklabel.
        #
        # Also ignore partitions on devices we do not support partitioning
        # of, like logical volumes.
        if not getattr(disk.format, "partitions", None) or \
           not disk.partitionable:
            # When we got here because the disk does not have a disklabel
            # format (ie a biosraid member), or because it is not
            # partitionable we want LVM to ignore this partition too
            if disk.format.type != "disklabel" or not disk.partitionable:
                devicelibs.lvm.lvm_cc_addFilterRejectRegexp(name)
            log.debug("ignoring partition %s" % name)
            return

        try:
            device = PartitionDevice(name, sysfsPath=sysfs_path,
                                     major=udev_device_get_major(info),
                                     minor=udev_device_get_minor(info),
                                     exists=True, parents=[disk])
        except DeviceError:
            # corner case sometime the kernel accepts a partition table
            # which gets rejected by parted, in this case we will
            # prompt to re-initialize the disk, so simply skip the
            # faulty partitions.
            return

        self._addDevice(device)
        return device

    def addUdevDiskDevice(self, info):
        name = udev_device_get_name(info)
        log_method_call(self, name=name)
        uuid = udev_device_get_uuid(info)
        sysfs_path = udev_device_get_sysfs_path(info)
        serial = udev_device_get_serial(info)
        bus = udev_device_get_bus(info)

        # udev doesn't always provide a vendor.
        vendor = udev_device_get_vendor(info)
        if not vendor:
            vendor = ""

        device = None

        kwargs = { "serial": serial, "vendor": vendor, "bus": bus }
        if udev_device_is_iscsi(info):
            diskType = iScsiDiskDevice
            kwargs["node"] = self.iscsi.getNode(
                                   udev_device_get_iscsi_name(info),
                                   udev_device_get_iscsi_address(info),
                                   udev_device_get_iscsi_port(info))
            kwargs["ibft"] = kwargs["node"] in self.iscsi.ibftNodes
            kwargs["initiator"] = self.iscsi.initiator
            log.debug("%s is an iscsi disk" % name)
        elif udev_device_is_fcoe(info):
            diskType = FcoeDiskDevice
            kwargs["nic"]        = udev_device_get_fcoe_nic(info)
            kwargs["identifier"] = udev_device_get_fcoe_identifier(info)
            log.debug("%s is an fcoe disk" % name)
        elif udev_device_get_md_container(info):
            diskType = MDRaidArrayDevice
            parentName = devicePathToName(udev_device_get_md_container(info))
            container = self.getDeviceByName(parentName)
            if not container:
                container_sysfs = "/class/block/" + parentName
                container_info = udev_get_block_device(container_sysfs)
                if not container_info:
                    log.error("failed to find md container %s at %s"
                                % (parentName, container_sysfs))
                return

                container = self.addUdevDevice(container_info)
                if not container:
                    log.error("failed to scan md container %s" % parentName)
                    return

            kwargs["parents"] = [container]
            kwargs["level"]  = udev_device_get_md_level(info)
            kwargs["memberDevices"] = int(udev_device_get_md_devices(info))
            kwargs["uuid"] = udev_device_get_md_uuid(info)
            kwargs["exists"]  = True
            del kwargs["serial"]
            del kwargs["vendor"]
            del kwargs["bus"]
        elif udev_device_is_dasd(info):
            diskType = DASDDevice
            kwargs["dasd"] = self.dasd
            kwargs["busid"] = udev_device_get_dasd_bus_id(info)
            kwargs["opts"] = {}

            for attr in ['readonly', 'use_diag', 'erplog', 'failfast']:
                kwargs["opts"][attr] = udev_device_get_dasd_flag(info, attr)

            log.debug("%s is a dasd device" % name)
        elif udev_device_is_zfcp(info):
            diskType = ZFCPDiskDevice

            for attr in ['hba_id', 'wwpn', 'fcp_lun']:
                kwargs[attr] = udev_device_get_zfcp_attribute(info, attr=attr)

            log.debug("%s is a zfcp device" % name)
        else:
            diskType = DiskDevice
            log.debug("%s is a disk" % name)

        device = diskType(name,
                          major=udev_device_get_major(info),
                          minor=udev_device_get_minor(info),
                          sysfsPath=sysfs_path, **kwargs)
        self._addDevice(device)
        return device

    def addUdevOpticalDevice(self, info):
        log_method_call(self)
        # XXX should this be RemovableDevice instead?
        #
        # Looks like if it has ID_INSTANCE=0:1 we can ignore it.
        device = OpticalDevice(udev_device_get_name(info),
                               major=udev_device_get_major(info),
                               minor=udev_device_get_minor(info),
                               sysfsPath=udev_device_get_sysfs_path(info),
                               vendor=udev_device_get_vendor(info),
                               model=udev_device_get_model(info))
        self._addDevice(device)
        return device

    def addUdevLoopDevice(self, info):
        name = udev_device_get_name(info)
        log_method_call(self, name=name)
        sysfs_path = udev_device_get_sysfs_path(info)
        sys_file = "/sys/%s/loop/backing_file" % sysfs_path
        backing_file = open(sys_file).read().strip()
        file_device = self.getDeviceByName(backing_file)
        if not file_device:
            file_device = FileDevice(backing_file, exists=True)
            self._addDevice(file_device)
        device = LoopDevice(name,
                            parents=[file_device],
                            sysfsPath=sysfs_path,
                            exists=True)
        if not self._cleanup or file_device not in self.diskImages.values():
            # don't allow manipulation of loop devices other than those
            # associated with disk images, and then only during cleanup
            file_device.controllable = False
            device.controllable = False
        self._addDevice(device)
        return device

    def addUdevDevice(self, info):
        name = udev_device_get_name(info)
        log_method_call(self, name=name, info=info)
        uuid = udev_device_get_uuid(info)
        sysfs_path = udev_device_get_sysfs_path(info)

        if self.isIgnored(info):
            log.debug("ignoring %s (%s)" % (name, sysfs_path))
            if name not in self._ignoredDisks:
                self.addIgnoredDisk(name)

            if udev_device_is_multipath_member(info):
                # last time we are seeing this mpath member is now, so make sure
                # LVM ignores its partitions too else a duplicate VG name could
                # harm us later during partition creation:
                if udev_device_is_dm(info):
                    path = "/dev/mapper/%s" % name
                else:
                    path = "/dev/%s" % name
                log.debug("adding partitions on %s to the lvm ignore list" % path)
                partitions_paths = []
                try:
                    partitions_paths = [p.path
                                       for p in parted.Disk(device=parted.Device(path=path)).partitions]
                except (_ped.IOException, _ped.DeviceException) as e:
                    log.error("Parted error scanning partitions on %s:" % path)
                    log.error(str(e))
                # slice off the "/dev/" part, lvm filter cares only about the rest
                partitions_paths = [p[5:] for p in partitions_paths]
                map(lvm.lvm_cc_addFilterRejectRegexp, partitions_paths)
            return

        log.debug("scanning %s (%s)..." % (name, sysfs_path))
        device = self.getDeviceByName(name)

        #
        # The first step is to either look up or create the device
        #
        if device:
            # we successfully looked up the device. skip to format handling.
            pass
        elif udev_device_is_loop(info):
            log.debug("%s is a loop device" % name)
            device = self.addUdevLoopDevice(info)
        elif udev_device_is_multipath_member(info):
            device = self.addUdevDiskDevice(info)
        elif udev_device_is_dm(info) and udev_device_is_dm_mpath(info):
            log.debug("%s is a multipath device" % name)
            device = self.addUdevDMDevice(info)
        elif udev_device_is_dm_lvm(info):
            log.debug("%s is an lvm logical volume" % name)
            device = self.addUdevLVDevice(info)
        elif udev_device_is_dm(info):
            log.debug("%s is a device-mapper device" % name)
            device = self.addUdevDMDevice(info)
        elif udev_device_is_md(info) and not udev_device_get_md_container(info):
            log.debug("%s is an md device" % name)
            if uuid:
                # try to find the device by uuid
                device = self.getDeviceByUuid(uuid)

            if device is None:
                device = self.addUdevMDDevice(info)
        elif udev_device_is_cdrom(info):
            log.debug("%s is a cdrom" % name)
            device = self.addUdevOpticalDevice(info)
        elif udev_device_is_biosraid_member(info) and udev_device_is_disk(info):
            log.debug("%s is part of a biosraid" % name)
            device = DiskDevice(name,
                            major=udev_device_get_major(info),
                            minor=udev_device_get_minor(info),
                            sysfsPath=sysfs_path, exists=True)
            self._addDevice(device)
        elif udev_device_is_disk(info):
            device = self.addUdevDiskDevice(info)
        elif udev_device_is_partition(info):
            log.debug("%s is a partition" % name)
            device = self.addUdevPartitionDevice(info)
        else:
            log.error("Unknown block device type for: %s" % name)
            return

        # If this device is protected, mark it as such now. Once the tree
        # has been populated, devices' protected attribute is how we will
        # identify protected devices.
        if device and device.name in self.protectedDevNames:
            device.protected = True

        # Don't try to do format handling on drives without media or
        # if we didn't end up with a device somehow.
        if not device or not device.mediaPresent:
            return

        # now handle the device's formatting
        self.handleUdevDeviceFormat(info, device)
        log.debug("got device: %s" % device)
        if device.format.type:
            log.debug("got format: %s" % device.format)
        device.originalFormat = device.format

    def handleUdevDiskLabelFormat(self, info, device):
        log_method_call(self, device=device.name)
        # if there is preexisting formatting on the device use it
        if getFormat(udev_device_get_format(info)).type is not None:
            log.debug("device %s does not contain a disklabel" % device.name)
            return

        if device.partitioned:
            # this device is already set up
            log.debug("disklabel format on %s already set up" % device.name)
            return

        try:
            device.setup()
        except Exception as e:
            log.debug("setup of %s failed: %s" % (device.name, e))
            log.warning("aborting disklabel handler for %s" % device.name)
            return

        # special handling for unsupported partitioned devices
        if not device.partitionable:
            try:
                format = getFormat("disklabel",
                                   device=device.path,
                                   exists=True)
            except InvalidDiskLabelError:
                pass
            else:
                if format.partitions:
                    # parted's checks for disklabel presence are less than
                    # rigorous, so we will assume that detected disklabels
                    # with no partitions are spurious
                    device.format = format
            return

        # if the disk contains protected partitions we will not wipe the
        # disklabel even if clearpart --initlabel was specified
        if not self.clearPartDisks or device.name in self.clearPartDisks:
            initlabel = self.reinitializeDisks
            sysfs_path = udev_device_get_sysfs_path(info)
            for protected in self.protectedDevNames:
                # check for protected partition
                _p = "/sys/%s/%s" % (sysfs_path, protected)
                if os.path.exists(os.path.normpath(_p)):
                    initlabel = False
                    break

                # check for protected partition on a device-mapper disk
                disk_name = re.sub(r'p\d+$', '', protected)
                if disk_name != protected and disk_name == device.name:
                    initlabel = False
                    break
        else:
            initlabel = False


        if self._cleanup:
            initcb = lambda: False
        elif self.zeroMbr:
            initcb = lambda: True
        else:
            description = device.description or device.model
            try:
                bypath = os.path.basename(deviceNameToDiskByPath(device.name))
            except DeviceNotFoundError:
                # some devices don't have a /dev/disk/by-path/ #!@#@!@#
                bypath = device.name

            initcb = lambda: self.intf.questionInitializeDisk(bypath,
                                                              description,
                                                              device.size)
        try:
            format = getFormat("disklabel",
                               device=device.path,
                               exists=not initlabel)
        except InvalidDiskLabelError:
            # if we have a cb function use it. else we ignore the device.
            if initcb is not None and initcb():
                format = getFormat("disklabel",
                                   device=device.path,
                                   exists=False)
            else:
                self._removeDevice(device)
                self.addIgnoredDisk(device.name)
                return

        if not format.exists:
            # if we just initialized a disklabel we should schedule
            # actions for destruction of the previous format and creation
            # of the new one
            self.registerAction(ActionDestroyFormat(device))
            self.registerAction(ActionCreateFormat(device, format))

            # If this is a mac-formatted disk we just initialized, make
            # sure the partition table partition gets added to the device
            # tree.
            if device.format.partedDisk.type == "mac" and \
               len(device.format.partitions) == 1:
                name = device.format.partitions[0].getDeviceNodeName()
                if not self.getDeviceByName(name):
                    partDevice = PartitionDevice(name, exists=True,
                                                 parents=[device])
                    self._addDevice(partDevice)

        else:
            device.format = format

    def handleUdevLUKSFormat(self, info, device):
        log_method_call(self, name=device.name, type=device.format.type)
        if not device.format.uuid:
            log.info("luks device %s has no uuid" % device.path)
            return

        # look up or create the mapped device
        if not self.getDeviceByName(device.format.mapName):
            passphrase = self.__luksDevs.get(device.format.uuid)
            if passphrase:
                device.format.passphrase = passphrase
            elif self._cleanup:
                # if we're only building the devicetree so that we can
                # tear down all of the devices we don't need a passphrase
                if device.format.status:
                    # this makes device.configured return True
                    device.format.passphrase = 'yabbadabbadoo'
            else:
                (passphrase, isglobal) = getLUKSPassphrase(self.intf,
                                                device,
                                                self.__passphrase)
                if isglobal and device.format.status:
                    self.__passphrase = passphrase

            luks_device = LUKSDevice(device.format.mapName,
                                     parents=[device],
                                     exists=True)
            try:
                luks_device.setup()
            except (LUKSError, CryptoError, DeviceError) as e:
                log.info("setup of %s failed: %s" % (device.format.mapName,
                                                     e))
                device.removeChild()
            else:
                self._addDevice(luks_device)
        else:
            log.warning("luks device %s already in the tree"
                        % device.format.mapName)

    def handleVgLvs(self, vg_device):
        """ Handle setup of the LV's in the vg_device
            return True if an LV was setup
            return False if there was an error, or no more LV's to setup
        """
        ret = False
        vg_name = vg_device.name
        lv_names = vg_device.lv_names
        lv_uuids = vg_device.lv_uuids
        lv_sizes = vg_device.lv_sizes
        lv_attr = vg_device.lv_attr

        if not vg_device.complete:
            log.warning("Skipping LVs for incomplete VG %s" % vg_name)
            return False

        if not lv_names:
            log.debug("no LVs listed for VG %s" % vg_name)
            return False

        # make a list of indices with snapshots at the end
        indices = range(len(lv_names))
        indices.sort(key=lambda i: lv_attr[i][0] in 'Ss')
        for index in indices:
            lv_name = lv_names[index]
            name = "%s-%s" % (vg_name, lv_name)
            if lv_attr[index][0] in 'Ss':
                log.debug("found lvm snapshot volume '%s'" % name)
                origin_name = devicelibs.lvm.lvorigin(vg_name, lv_name)
                if not origin_name:
                    log.error("lvm snapshot '%s-%s' has unknown origin"
                                % (vg_name, lv_name))
                    continue

                origin = self.getDeviceByName("%s-%s" % (vg_name,
                                                         origin_name))
                if not origin:
                    if origin_name.endswith("_vorigin]"):
                        log.info("snapshot volume '%s' has vorigin" % name)
                        vg_device.voriginSnapshots[lv_name] = lv_sizes[index]
                    else:
                        log.warning("snapshot lv '%s' origin lv '%s-%s' "
                                    "not found" % (name,
                                                   vg_name, origin_name))
                    continue

                log.debug("adding %dMB to %s snapshot total"
                            % (lv_sizes[index], origin.name))
                origin.snapshotSpace += lv_sizes[index]
                continue
            elif lv_attr[index][0] in 'Iilv':
                # skip mirror images, log volumes, and vorigins
                continue

            log_size = 0
            if lv_attr[index][0] in 'Mm':
                stripes = 0
                # identify mirror stripes/copies and mirror logs
                for (j, _lvname) in enumerate(lv_names):
                    if lv_attr[j][0] not in 'Iil':
                        continue

                    if _lvname == "[%s_mlog]" % lv_name:
                        log_size = lv_sizes[j]
                    elif _lvname.startswith("[%s_mimage_" % lv_name):
                        stripes += 1
            else:
                stripes = 1

            lv_dev = self.getDeviceByName(name)
            if lv_dev is None:
                lv_uuid = lv_uuids[index]
                lv_size = lv_sizes[index]
                lv_device = LVMLogicalVolumeDevice(lv_name,
                                                   vg_device,
                                                   uuid=lv_uuid,
                                                   size=lv_size,
                                                   stripes=stripes,
                                                   logSize=log_size,
                                                   exists=True)
                self._addDevice(lv_device)
                lv_device.setup()
                ret = True

        return ret

    def handleUdevLVMPVFormat(self, info, device):
        log_method_call(self, name=device.name, type=device.format.type)
        # lookup/create the VG and LVs
        try:
            vg_name = udev_device_get_vg_name(info)
        except KeyError:
            # no vg name means no vg -- we're done with this pv
            return

        vg_device = self.getDeviceByName(vg_name)
        if vg_device:
            vg_device._addDevice(device)
        else:
            try:
                vg_uuid = udev_device_get_vg_uuid(info)
                vg_size = udev_device_get_vg_size(info)
                vg_free = udev_device_get_vg_free(info)
                pe_size = udev_device_get_vg_extent_size(info)
                pe_count = udev_device_get_vg_extent_count(info)
                pe_free = udev_device_get_vg_free_extents(info)
                pv_count = udev_device_get_vg_pv_count(info)
            except (KeyError, ValueError) as e:
                log.warning("invalid data for %s: %s" % (device.name, e))
                return

            vg_device = LVMVolumeGroupDevice(vg_name,
                                             device,
                                             uuid=vg_uuid,
                                             size=vg_size,
                                             free=vg_free,
                                             peSize=pe_size,
                                             peCount=pe_count,
                                             peFree=pe_free,
                                             pvCount=pv_count,
                                             exists=True)
            self._addDevice(vg_device)

        # Now we add any lv info found in this pv to the vg_device, we
        # do this for all pvs as pvs only contain lv info for lvs which they
        # contain themselves
        try:
            lv_names = udev_device_get_lv_names(info)
            lv_uuids = udev_device_get_lv_uuids(info)
            lv_sizes = udev_device_get_lv_sizes(info)
            lv_attr = udev_device_get_lv_attr(info)
        except KeyError as e:
            log.warning("invalid data for %s: %s" % (device.name, e))
            return

        for i in range(len(lv_names)):
            # Skip empty and already added lvs
            if not lv_names[i] or lv_names[i] in vg_device.lv_names:
                continue

            vg_device.lv_names.append(lv_names[i])
            vg_device.lv_uuids.append(lv_uuids[i])
            vg_device.lv_sizes.append(lv_sizes[i])
            vg_device.lv_attr.append(lv_attr[i])

    def handleUdevMDMemberFormat(self, info, device):
        log_method_call(self, name=device.name, type=device.format.type)
        # either look up or create the array device
        name = udev_device_get_name(info)
        sysfs_path = udev_device_get_sysfs_path(info)

        md_array = self.getDeviceByUuid(device.format.mdUuid)
        if device.format.mdUuid and md_array:
            md_array._addDevice(device)
        else:
            # create the array with just this one member
            try:
                # level is reported as, eg: "raid1"
                md_level = udev_device_get_md_level(info)
                md_devices = int(udev_device_get_md_devices(info))
                md_uuid = udev_device_get_md_uuid(info)
            except (KeyError, ValueError) as e:
                log.warning("invalid data for %s: %s" % (name, e))
                return

            md_name = None
            minor = None

            # check the list of devices udev knows about to see if the array
            # this device belongs to is already active
            for dev in self.topology.devices_iter():
                if not udev_device_is_md(dev):
                    continue

                try:
                    dev_uuid = udev_device_get_md_uuid(dev)
                    dev_level = udev_device_get_md_level(dev)
                except KeyError:
                    continue

                if dev_uuid is None or dev_level is None:
                    continue

                if dev_uuid == md_uuid and dev_level == md_level:
                    md_name = udev_device_get_name(dev)
                    minor = udev_device_get_minor(dev)
                    break

            if not md_name:
                # try to name the array based on the preferred minor
                md_info = devicelibs.mdraid.mdexamine(device.path)
                md_path = md_info.get("device", "")
                md_name = devicePathToName(md_info.get("device", ""))
                if md_name:
                    try:
                        # md_name can be either md# or md/#
                        if md_name.startswith("md/"):
                            minor = int(md_name[3:])     # strip off leading "md/"
                            md_name = "md%d" % minor     # use a regular md# name
                        else:
                            minor = int(md_name[2:])     # strip off leading "md"
                    except (IndexError, ValueError):
                        minor = None
                        md_name = None
                    else:
                        array = self.getDeviceByName(md_name)
                        if array and array.uuid != md_uuid:
                            md_name = None

                if not md_name:
                    # if we don't have a name yet, find the first unused minor
                    minor = 0
                    while True:
                        if self.getDeviceByName("md%d" % minor):
                            minor += 1
                        else:
                            break

                    md_name = "md%d" % minor

            log.debug("using name %s for md array containing member %s"
                        % (md_name, device.name))
            md_array = MDRaidArrayDevice(md_name,
                                         level=md_level,
                                         minor=minor,
                                         memberDevices=md_devices,
                                         uuid=md_uuid,
                                         sysfsPath=sysfs_path,
                                         exists=True)
            md_array._addDevice(device)
            self._addDevice(md_array)

    def handleMultipathMemberFormat(self, info, device):
        log_method_call(self, name=device.name, type=device.format.type)

        name = udev_device_get_multipath_name(info)
        if self.__multipaths.has_key(name):
            mp = self.__multipaths[name]
            mp.addParent(device)
        else:
            mp = MultipathDevice(name, info, parents=[device])
            self.__multipaths[name] = mp

    def handleUdevDMRaidMemberFormat(self, info, device):
        log_method_call(self, name=device.name, type=device.format.type)
        name = udev_device_get_name(info)
        sysfs_path = udev_device_get_sysfs_path(info)
        uuid = udev_device_get_uuid(info)
        major = udev_device_get_major(info)
        minor = udev_device_get_minor(info)

        def _all_ignored(rss):
            retval = True
            for rs in rss:
                if rs.name not in self._ignoredDisks:
                    retval = False
                    break
            return retval

        # Have we already created the DMRaidArrayDevice?
        rss = block.getRaidSetFromRelatedMem(uuid=uuid, name=name,
                                            major=major, minor=minor)
        if len(rss) == 0:
            # we ignore the device in the hope that all the devices
            # from this set will be ignored.
            self.unusedRaidMembers.append(device.name)
            self.addIgnoredDisk(device.name)
            return

        # We ignore the device if all the rss are in self._ignoredDisks
        if _all_ignored(rss):
            self.addIgnoredDisk(device.name)
            return

        for rs in rss:
            dm_array = self.getDeviceByName(rs.name)
            if dm_array is not None:
                # We add the new device.
                dm_array._addDevice(device)
            else:
                # Activate the Raid set.
                rs.activate(mknod=True)
                dm_array = DMRaidArrayDevice(rs.name,
                                             raidSet=rs,
                                             parents=[device])

                self._addDevice(dm_array)

                # Wait for udev to scan the just created nodes, to avoid a race
                # with the udev_get_block_device() call below.
                udev_settle()

                # Get the DMRaidArrayDevice a DiskLabel format *now*, in case
                # its partitions get scanned before it does.
                dm_array.updateSysfsPath()
                dm_array_info = udev_get_block_device(dm_array.sysfsPath)
                self.handleUdevDiskLabelFormat(dm_array_info, dm_array)

                # Use the rs's object on the device.
                # pyblock can return the memebers of a set and the
                # device has the attribute to hold it.  But ATM we
                # are not really using it. Commenting this out until
                # we really need it.
                #device.format.raidmem = block.getMemFromRaidSet(dm_array,
                #        major=major, minor=minor, uuid=uuid, name=name)

    def handleUdevDeviceFormat(self, info, device):
        log_method_call(self, name=getattr(device, "name", None))
        name = udev_device_get_name(info)
        sysfs_path = udev_device_get_sysfs_path(info)
        uuid = udev_device_get_uuid(info)
        label = udev_device_get_label(info)
        format_type = udev_device_get_format(info)
        serial = udev_device_get_serial(info)

        # Now, if the device is a disk, see if there is a usable disklabel.
        # If not, see if the user would like to create one.
        # XXX ignore disklabels on multipath or biosraid member disks
        if not udev_device_is_biosraid_member(info) and \
           not udev_device_is_multipath_member(info):
            self.handleUdevDiskLabelFormat(info, device)
            if device.partitioned or self.isIgnored(info) or \
               (not device.partitionable and
                device.format.type == "disklabel"):
                # If the device has a disklabel, or the user chose not to
                # create one, we are finished with this device. Otherwise
                # it must have some non-disklabel formatting, in which case
                # we fall through to handle that.
                return

        format = None
        if (not device) or (not format_type) or device.format.type:
            # this device has no formatting or it has already been set up
            # FIXME: this probably needs something special for disklabels
            log.debug("no type or existing type for %s, bailing" % (name,))
            return

        # set up the common arguments for the format constructor
        args = [format_type]
        kwargs = {"uuid": uuid,
                  "label": label,
                  "device": device.path,
                  "serial": serial,
                  "exists": True}

        # set up type-specific arguments for the format constructor
        if format_type == "multipath_member":
            kwargs["multipath_members"] = self.getDevicesBySerial(serial)
        elif format_type == "crypto_LUKS":
            # luks/dmcrypt
            kwargs["name"] = "luks-%s" % uuid
        elif format_type in formats.mdraid.MDRaidMember._udevTypes:
            # mdraid
            try:
                kwargs["mdUuid"] = udev_device_get_md_uuid(info)
            except KeyError:
                log.debug("mdraid member %s has no md uuid" % name)
            kwargs["biosraid"] = udev_device_is_biosraid_member(info)
        elif format_type == "LVM2_member":
            # lvm
            try:
                kwargs["vgName"] = udev_device_get_vg_name(info)
            except KeyError as e:
                log.debug("PV %s has no vg_name" % name)
            try:
                kwargs["vgUuid"] = udev_device_get_vg_uuid(info)
            except KeyError:
                log.debug("PV %s has no vg_uuid" % name)
            try:
                kwargs["peStart"] = udev_device_get_pv_pe_start(info)
            except KeyError:
                log.debug("PV %s has no pe_start" % name)
        elif format_type == "vfat":
            # efi magic
            if isinstance(device, PartitionDevice) and device.bootable:
                efi = formats.getFormat("efi")
                if efi.minSize <= device.size <= efi.maxSize:
                    args[0] = "efi"
                    kwargs['mountpoint'] = "/boot/efi"
        elif format_type == "hfs":
            # apple bootstrap magic
            if isinstance(device, PartitionDevice) and device.bootable:
                apple = formats.getFormat("appleboot")
                if apple.minSize <= device.size <= apple.maxSize:
                    args[0] = "appleboot"

        try:
            log.debug("type detected on '%s' is '%s'" % (name, format_type,))
            device.format = formats.getFormat(*args, **kwargs)
        except FSError:
            log.debug("type '%s' on '%s' invalid, assuming no format" %
                      (format_type, name,))
            device.format = formats.DeviceFormat()
            return

        if shouldClear(device, self.clearPartType,
                       clearPartDisks=self.clearPartDisks):
            # if this is a device that will be cleared by clearpart,
            # don't bother with format-specific processing
            return

        #
        # now do any special handling required for the device's format
        #
        if device.format.type == "luks":
            self.handleUdevLUKSFormat(info, device)
        elif device.format.type == "mdmember":
            self.handleUdevMDMemberFormat(info, device)
        elif device.format.type == "dmraidmember":
            self.handleUdevDMRaidMemberFormat(info, device)
        elif device.format.type == "lvmpv":
            self.handleUdevLVMPVFormat(info, device)
        elif device.format.type == "multipath_member":
            self.handleMultipathMemberFormat(info, device)

    def updateDeviceFormat(self, device):
        log.debug("updating format of device: %s" % device)
        iutil.notify_kernel("/sys%s" % device.sysfsPath)
        udev_settle()
        info = udev_get_device(device.sysfsPath)
        self.handleUdevDeviceFormat(info, device)
        if device.format.type:
            log.debug("got format: %s" % device.format)

    def _handleInconsistencies(self):
        def reinitializeVG(vg):
            # First we remove VG data
            try:
                vg.destroy()
            except DeviceError:
                # the pvremoves will finish the job.
                log.debug("There was an error destroying the VG %s." % vg.name)

            # remove VG device from list.
            self._removeDevice(vg)

            for parent in vg.parents:
                parent.format.destroy()

                # Give the vg the a default format
                kwargs = {"device": parent.path,
                          "exists": parent.exists}
                parent.format = formats.getFormat(*[""], **kwargs)

        def leafInconsistencies(device):
            if device.type == "lvmvg":
                if device.complete:
                    return

                paths = []
                for parent in device.parents:
                    paths.append(parent.path)

                # if zeroMbr is true don't ask.
                if not self._cleanup and \
                   (self.zeroMbr or
                    self.intf.questionReinitInconsistentLVM(pv_names=paths,
                                                            vg_name=device.name)):
                    reinitializeVG(device)
                else:
                    # The user chose not to reinitialize.
                    # hopefully this will ignore the vg components too.
                    self._removeDevice(device)
                    devicelibs.lvm.lvm_cc_addFilterRejectRegexp(device.name)
                    devicelibs.lvm.blacklistVG(device.name)
                    for parent in device.parents:
                        if parent.type == "partition":
                            parent.immutable = \
                                _("This partition is part of an inconsistent LVM Volume Group.")
                        else:
                            self._removeDevice(parent, moddisk=False)
                            self.addIgnoredDisk(parent.name)
                        devicelibs.lvm.lvm_cc_addFilterRejectRegexp(parent.name)

        # Address the inconsistencies present in the tree leaves.
        for leaf in self.leaves:
            leafInconsistencies(leaf)

        # Check for unused BIOS raid members, unused dmraid members are added
        # to self.unusedRaidMembers as they are processed, extend this list
        # with unused mdraid BIOS raid members
        for c in self.getDevicesByType("mdcontainer"):
            if c.kids == 0:
                self.unusedRaidMembers.extend(map(lambda m: m.name, c.devices))

        if not self._cleanup:
            self.intf.unusedRaidMembersWarning(self.unusedRaidMembers)

    def _setupLvs(self):
        ret = False

        for device in self.getDevicesByType("lvmvg"):
            if self.handleVgLvs(device):
                ret = True

        return ret

    def setupDiskImages(self):
        """ Set up devices to represent the disk image files. """
        for (name, path) in self.diskImages.items():
            log.info("setting up disk image file '%s' as '%s'" % (path, name))
            try:
                filedev = FileDevice(path, exists=True)
                filedev.setup()
                log.debug("%s" % filedev)

                loop_name = devicelibs.loop.get_loop_name(filedev.path)
                loop_sysfs = None
                if loop_name:
                    loop_sysfs = "/class/block/%s" % loop_name
                loopdev = LoopDevice(name=loop_name,
                                     parents=[filedev],
                                     sysfsPath=loop_sysfs,
                                     exists=True)
                loopdev.setup()
                log.debug("%s" % loopdev)
                dmdev = DMLinearDevice(name,
                                       dmUuid="ANACONDA-%s" % name,
                                       parents=[loopdev],
                                       exists=True)
                dmdev.setup()
                dmdev.updateSysfsPath()
                log.debug("%s" % dmdev)
            except (ValueError, DeviceError) as e:
                log.error("failed to set up disk image: %s" % e)
            else:
                self._addDevice(filedev)
                self._addDevice(loopdev)
                self._addDevice(dmdev)
                info = udev_get_block_device(dmdev.sysfsPath)
                self.addUdevDevice(info)

    def backupConfigs(self, restore=False):
        """ Create a backup copies of some storage config files. """
        configs = ["/etc/mdadm.conf", "/etc/multipath.conf"]
        for cfg in configs:
            if restore:
                src = cfg + ".anacbak"
                dst = cfg
                func = os.rename
                op = "restore from backup"
            else:
                src = cfg
                dst = cfg + ".anacbak"
                func = shutil.copy2
                op = "create backup copy"

            if os.access(dst, os.W_OK):
                try:
                    os.unlink(dst)
                except OSError as e:
                    msg = str(e)
                    log.info("failed to remove %s: %s" % (dst, msg))

            if os.access(src, os.W_OK):
                # copy the config to a backup with extension ".anacbak"
                try:
                    func(src, dst)
                except (IOError, OSError) as e:
                    msg = str(e)
                    log.error("failed to %s of %s: %s" % (op, cfg, msg))
            elif restore:
                # remove the config since we created it
                log.info("removing anaconda-created %s" % cfg)
                try:
                    os.unlink(cfg)
                except OSError as e:
                    msg = str(e)
                    log.error("failed to remove %s: %s" % (cfg, msg))
            else:
                # don't try to backup non-existent configs
                log.info("not going to %s of non-existent %s" % (op, cfg))

    def restoreConfigs(self):
        self.backupConfigs(restore=True)

    def populate(self, cleanupOnly=False):
        """ Locate all storage devices. """
        self.backupConfigs()
        if cleanupOnly:
            self._cleanup = True

        try:
            self._populate()
        except Exception:
            raise
        finally:
            self.restoreConfigs()

    def _populate(self):
        log.debug("DeviceTree.populate: ignoredDisks is %s ; exclusiveDisks is %s"
                    % (self._ignoredDisks, self.exclusiveDisks))

        self.setupDiskImages()

        # mark the tree as unpopulated so exception handlers can tell the
        # exception originated while finding storage devices
        self.populated = False

        # resolve the protected device specs to device names
        for spec in self.protectedDevSpecs:
            name = udev_resolve_devspec(spec)
            if name:
                self.protectedDevNames.append(name)

        # FIXME: the backing dev for the live image can't be used as an
        # install target.  note that this is a little bit of a hack
        # since we're assuming that /dev/live will exist
        if os.path.exists("/dev/live") and \
           stat.S_ISBLK(os.stat("/dev/live")[stat.ST_MODE]):
            livetarget = devicePathToName(os.path.realpath("/dev/live"))
            log.info("%s looks to be the live device; marking as protected"
                     % (livetarget,))
            self.protectedDevNames.append(livetarget)

        cfg = self.__multipathConfigWriter.write()
        open("/etc/multipath.conf", "w+").write(cfg)
        del cfg

        self.topology = devicelibs.mpath.MultipathTopology(udev_get_block_devices())
        log.info("devices to scan: %s" %
                 [d['name'] for d in self.topology.devices_iter()])
        old_devices = {}
        for dev in self.topology.devices_iter():
            old_devices[dev['name']] = dev
            self.addUdevDevice(dev)

        # Having found all the disks, we can now find all the multipaths built
        # upon them.
        whitelist = []
        mpaths = self.__multipaths.values()
        mpaths.sort(key=lambda d: d.name)
        for mp in mpaths:
            log.info("adding mpath device %s" % mp.name)
            mp.setup()
            mp.updateSysfsPath()
            mp_info = udev_get_block_device(mp.sysfsPath)
            if mp_info is None or self.isIgnored(mp_info):
                mp.teardown()
                continue

            whitelist.append(mp.name)
            for p in mp.parents:
                whitelist.append(p.name)
            self.__multipathConfigWriter.addMultipathDevice(mp)
            self._addDevice(mp)
            self.addUdevDevice(mp_info)
        for d in self.devices:
            if not d.name in whitelist:
                self.__multipathConfigWriter.addBlacklistDevice(d)
        cfg = self.__multipathConfigWriter.write()
        open("/etc/multipath.conf", "w+").write(cfg)
        del cfg

        # Now, loop and scan for devices that have appeared since the two above
        # blocks or since previous iterations.
        while True:
            devices = []
            new_devices = udev_get_block_devices()

            for new_device in new_devices:
                if not old_devices.has_key(new_device['name']):
                    old_devices[new_device['name']] = new_device
                    devices.append(new_device)

            if len(devices) == 0:
                # nothing is changing -- time to setup lvm lvs and scan them
                # we delay this till all other devices are scanned so that
                # 1) the lvm filter for ignored disks is completely setup
                # 2) we have checked all devs for duplicate vg names
                if self._setupLvs():
                    # remove any logical volume devices from old_devices so
                    # they will be re-scanned to get their formatting handled
                    for (old_name, old_device) in old_devices.items():
                        if udev_device_is_dm_lvm(old_device):
                            del old_devices[old_name]
                    continue
                # nothing is changing -- we are finished building devices
                break

            log.info("devices to scan: %s" % [d['name'] for d in devices])
            for dev in devices:
                self.addUdevDevice(dev)

        self.populated = True

        # After having the complete tree we make sure that the system
        # inconsistencies are ignored or resolved.
        self._handleInconsistencies()

        self.teardownAll()

    def teardownAll(self):
        """ Run teardown methods on all devices. """
        for device in self.leaves:
            try:
                device.teardown(recursive=True)
            except StorageError as e:
                log.info("teardown of %s failed: %s" % (device.name, e))

    def setupAll(self):
        """ Run setup methods on all devices. """
        for device in self.leaves:
            try:
                device.setup()
            except DeviceError as (msg, name):
                log.debug("setup of %s failed: %s" % (device.name, msg))

    def getDeviceBySysfsPath(self, path):
        if not path:
            return None

        found = None
        for device in self._devices:
            if device.sysfsPath == path:
                found = device
                break

        return found

    def getDeviceByUuid(self, uuid):
        if not uuid:
            return None

        found = None
        for device in self._devices:
            if device.uuid == uuid:
                found = device
                break
            elif device.format.uuid == uuid:
                found = device
                break

        return found

    def getDevicesBySerial(self, serial):
        devices = []
        for device in self._devices:
            if not hasattr(device, "serial"):
                log.warning("device %s has no serial attr" % device.name)
                continue
            if device.serial == serial:
                devices.append(device)
        return devices

    def getDeviceByLabel(self, label):
        if not label:
            return None

        found = None
        for device in self._devices:
            _label = getattr(device.format, "label", None)
            if not _label:
                continue

            if _label == label:
                found = device
                break

        return found

    def getDeviceByName(self, name):
        log_method_call(self, name=name)
        if not name:
            log_method_return(self, None)
            return None

        found = None
        for device in self._devices:
            if device.name == name:
                found = device
                break
            elif (device.type == "lvmlv" or device.type == "lvmvg") and \
                    device.name == name.replace("--","-"):
                found = device
                break

        log_method_return(self, found)
        return found

    def getDeviceByPath(self, path):
        log_method_call(self, path=path)
        if not path:
            log_method_return(self, None)
            return None

        found = None
        for device in self._devices:
            if device.path == path:
                found = device
                break
            elif (device.type == "lvmlv" or device.type == "lvmvg") and \
                    device.path == path.replace("--","-"):
                found = device
                break

        log_method_return(self, found)
        return found

    def getDevicesByType(self, device_type):
        # TODO: expand this to catch device format types
        return [d for d in self._devices if d.type == device_type]

    def getDevicesByInstance(self, device_class):
        return [d for d in self._devices if isinstance(d, device_class)]

    @property
    def devices(self):
        """ List of device instances """
        devices = []
        for device in self._devices:
            if device.path in [d.path for d in devices] and \
               not isinstance(device, NoDevice):
                raise DeviceTreeError("duplicate paths in device tree")

            devices.append(device)

        return devices

    @property
    def filesystems(self):
        """ List of filesystems. """
        #""" Dict with mountpoint keys and filesystem values. """
        filesystems = []
        for dev in self.leaves:
            if dev.format and getattr(dev.format, 'mountpoint', None):
                filesystems.append(dev.format)

        return filesystems

    @property
    def uuids(self):
        """ Dict with uuid keys and Device values. """
        uuids = {}
        for dev in self._devices:
            try:
                uuid = dev.uuid
            except AttributeError:
                uuid = None

            if uuid:
                uuids[uuid] = dev

            try:
                uuid = dev.format.uuid
            except AttributeError:
                uuid = None

            if uuid:
                uuids[uuid] = dev

        return uuids

    @property
    def labels(self):
        """ Dict with label keys and Device values.

            FIXME: duplicate labels are a possibility
        """
        labels = {}
        for dev in self._devices:
            if dev.format and getattr(dev.format, "label", None):
                labels[dev.format.label] = dev

        return labels

    @property
    def leaves(self):
        """ List of all devices upon which no other devices exist. """
        leaves = [d for d in self._devices if d.isleaf]
        return leaves

    def getChildren(self, device):
        """ Return a list of a device's children. """
        return [c for c in self._devices if device in c.parents]

    def resolveDevice(self, devspec, blkidTab=None, cryptTab=None):
        # find device in the tree
        device = None
        if devspec.startswith("UUID="):
            # device-by-uuid
            uuid = devspec.partition("=")[2]
            if ((uuid.startswith('"') and uuid.endswith('"')) or
                (uuid.startswith("'") and uuid.endswith("'"))):
                uuid = uuid[1:-1]
            device = self.uuids.get(uuid)
            if device is None:
                log.error("failed to resolve device %s" % devspec)
        elif devspec.startswith("LABEL="):
            # device-by-label
            label = devspec.partition("=")[2]
            if ((label.startswith('"') and label.endswith('"')) or
                (label.startswith("'") and label.endswith("'"))):
                label = label[1:-1]
            device = self.labels.get(label)
            if device is None:
                log.error("failed to resolve device %s" % devspec)
        elif devspec.startswith("/dev/"):
            if devspec.startswith("/dev/disk/"):
                devspec = os.path.realpath(devspec)

                if devspec.startswith("/dev/dm-"):
                    dm_name = devicelibs.dm.name_from_dm_node(devspec[5:])
                    if dm_name:
                        devspec = "/dev/mapper/" + dm_name

            # device path
            device = self.getDeviceByPath(devspec)
            if device is None:
                if blkidTab:
                    # try to use the blkid.tab to correlate the device
                    # path with a UUID
                    blkidTabEnt = blkidTab.get(devspec)
                    if blkidTabEnt:
                        log.debug("found blkid.tab entry for '%s'" % devspec)
                        uuid = blkidTabEnt.get("UUID")
                        if uuid:
                            device = self.getDeviceByUuid(uuid)
                            if device:
                                devstr = device.name
                            else:
                                devstr = "None"
                            log.debug("found device '%s' in tree" % devstr)
                        if device and device.format and \
                           device.format.type == "luks":
                            map_name = device.format.mapName
                            log.debug("luks device; map name is '%s'" % map_name)
                            mapped_dev = self.getDeviceByName(map_name)
                            if mapped_dev:
                                device = mapped_dev

                if device is None and cryptTab and \
                   devspec.startswith("/dev/mapper/"):
                    # try to use a dm-crypt mapping name to 
                    # obtain the underlying device, possibly
                    # using blkid.tab
                    cryptTabEnt = cryptTab.get(devspec.split("/")[-1])
                    if cryptTabEnt:
                        luks_dev = cryptTabEnt['device']
                        try:
                            device = self.getChildren(luks_dev)[0]
                        except IndexError as e:
                            pass
                elif device is None:
                    # dear lvm: can we please have a few more device nodes
                    #           for each logical volume?
                    #           three just doesn't seem like enough.
                    name = devspec[5:]      # strip off leading "/dev/"
                    (vg_name, slash, lv_name) = name.partition("/")
                    if lv_name and not "/" in lv_name:
                        # looks like we may have one
                        lv = "%s-%s" % (vg_name, lv_name)
                        device = self.getDeviceByName(lv)

        if device:
            log.debug("resolved '%s' to '%s' (%s)" % (devspec, device.name, device.type))
        else:
            log.debug("failed to resolve '%s'" % devspec)
        return device
