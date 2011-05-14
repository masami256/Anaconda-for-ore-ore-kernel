# devices.py
# Device classes for anaconda's storage configuration module.
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


"""
    Device classes for use by anaconda.

    This is the hierarchy of device objects that anaconda will use for
    managing storage devices in the system. These classes will
    individually make use of external support modules as needed to
    perform operations specific to the type of device they represent.

    TODO:
        - see how to do network devices (NetworkManager may help)
          - perhaps just a wrapper here
        - document return values of all methods/functions
        - find out what other kinds of wild and crazy devices we need to
          represent here (iseries? xen? more mainframe? mac? ps?)
            - PReP
              - this is a prime candidate for a PseudoDevice
            - DASD
            - ZFCP
            - XEN

    What specifications do we allow?              new        existing
        partitions                              
            usage                                  +            +
                filesystem, partition type are implicit
            mountpoint                             +            +
            size
                exact                              +            -
                range                              +            -
                resize                             -            +
            format                                 -            +
            encryption                             +            +

            disk                                                 
                exact                              +            -
                set                                +            -
                    how will we specify this?
                        partition w/ multiple parents cannot otherwise occur
            primary                                +            -

        mdraid sets
            filesystem (*)                         +            +
            mountpoint                             +            +
            size?                                                
            format                                 -            +
            encryption                             +            +

            level                                  +            ? 
            device minor                           +            ? 
            member devices                         +            ? 
            spares                                 +            ? 
            name?
            bitmap? (boolean)                      +            -

        volume groups
            name                                   +            - 
            member pvs                             +            +
            pesize                                 +            ?

        logical volumes
            filesystem                             +            +
            mountpoint                             +            +
            size
                exact                              +            ?
            format                                 -            +
            encryption                             +            +

            name                                   +            ?
            vgname                                 +            ?


"""

import os
import math
import copy
import time

# device backend modules
from devicelibs import mdraid
from devicelibs import lvm
from devicelibs import dm
from devicelibs import loop
import parted
import _ped
import block

from errors import *
from pyanaconda.iutil import notify_kernel, numeric_type
from pyanaconda.flags import flags
from pyanaconda.anaconda_log import log_method_call
from udev import *
from formats import get_device_format_class, getFormat, DeviceFormat

import gettext
_ = lambda x: gettext.ldgettext("anaconda", x)
P_ = lambda x, y, z: gettext.ldngettext("anaconda", x, y, z)

import logging
log = logging.getLogger("storage")

def get_device_majors():
    majors = {}
    for line in open("/proc/devices").readlines():
        try:
            (major, device) = line.split()
        except ValueError:
            continue
        try:
            majors[int(major)] = device
        except ValueError:
            continue
    return majors
device_majors = get_device_majors()


def devicePathToName(devicePath):
    if devicePath.startswith("/dev/"):
        name = devicePath[5:]
    else:
        name = devicePath

    if name.startswith("mapper/"):
        name = name[7:]

    return name


def deviceNameToDiskByPath(deviceName=None):
    if not deviceName:
        return ""

    ret = None
    for dev in udev_get_block_devices():
        if udev_device_get_name(dev) == deviceName:
            ret = udev_device_get_by_path(dev)
            break

    if ret:
        return ret
    raise DeviceNotFoundError(deviceName)

class Device(object):
    """ A generic device.

        Device instances know which devices they depend upon (parents
        attribute). They do not know which devices depend upon them, but
        they do know whether or not they have any dependent devices
        (isleaf attribute).

        A Device's setup method should set up all parent devices as well
        as the device itself. It should not run the resident format's
        setup method.

            Which Device types rely on their parents' formats being active?
                DMCryptDevice

        A Device's teardown method should accept the keyword argument
        recursive, which takes a boolean value and indicates whether or
        not to recursively close parent devices.

        A Device's create method should create all parent devices as well
        as the device itself. It should also run the Device's setup method
        after creating the device. The create method should not create a
        device's resident format.

            Which device type rely on their parents' formats to be created
            before they can be created/assembled?
                VolumeGroup
                DMCryptDevice

        A Device's destroy method should destroy any resident format
        before destroying the device itself.

    """

    # This is a counter for generating unique ids for Devices.
    _id = 0

    _type = "generic device"
    _packages = []
    _services = []

    def __init__(self, name, parents=None):
        """ Create a Device instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                parents -- a list of required Device instances

        """
        self._name = name
        if parents is None:
            parents = []
        elif not isinstance(parents, list):
            raise ValueError("parents must be a list of Device instances")
        self.parents = parents
        self.kids = 0

        # Set this instance's id and increment the counter.
        self.id = Device._id
        Device._id += 1

        for parent in self.parents:
            parent.addChild()

    def __deepcopy__(self, memo):
        """ Create a deep copy of a Device instance.

            We can't do copy.deepcopy on parted objects, which is okay.
            For these parted objects, we just do a shallow copy.
        """
        new = self.__class__.__new__(self.__class__)
        memo[id(self)] = new
        dont_copy_attrs = ('_raidSet',)
        shallow_copy_attrs = ('_partedDevice', '_partedPartition')
        for (attr, value) in self.__dict__.items():
            if attr in dont_copy_attrs:
                setattr(new, attr, value)
            elif attr in shallow_copy_attrs:
                setattr(new, attr, copy.copy(value))
            else:
                setattr(new, attr, copy.deepcopy(value, memo))

        return new

    def __str__(self):
        s = ("%(type)s instance (%(id)s) --\n"
             "  name = %(name)s  status = %(status)s"
             "  parents = %(parents)s\n"
             "  kids = %(kids)s\n"
             "  id = %(dev_id)s\n" %
             {"type": self.__class__.__name__, "id": "%#x" % id(self),
              "name": self.name, "parents": self.parents, "kids": self.kids,
              "status": self.status, "dev_id": self.id})
        return s

    @property
    def dict(self):
        d =  {"type": self.type, "name": self.name,
              "parents": [p.name for p in self.parents]}
        return d

    def writeKS(self, f, preexisting=False, noformat=False, s=None):
        return

    def removeChild(self):
        log_method_call(self, name=self.name, kids=self.kids)
        self.kids -= 1

    def addChild(self):
        log_method_call(self, name=self.name, kids=self.kids)
        self.kids += 1

    def setup(self, intf=None):
        """ Open, or set up, a device. """
        raise NotImplementedError("setup method not defined for Device")

    def teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        raise NotImplementedError("teardown method not defined for Device")

    def create(self, intf=None):
        """ Create the device. """
        raise NotImplementedError("create method not defined for Device")

    def destroy(self):
        """ Destroy the device. """
        raise NotImplementedError("destroy method not defined for Device")

    def setupParents(self, orig=False):
        """ Run setup method of all parent devices. """
        log_method_call(self, name=self.name, orig=orig, kids=self.kids)
        for parent in self.parents:
            parent.setup(orig=orig)

    def teardownParents(self, recursive=None):
        """ Run teardown method of all parent devices. """
        for parent in self.parents:
            parent.teardown(recursive=recursive)

    def dependsOn(self, dep):
        """ Return True if this device depends on dep. """
        # XXX does a device depend on itself?
        if dep in self.parents:
            return True

        for parent in self.parents:
            if parent.dependsOn(dep):
                return True

        return False

    def dracutSetupString(self):
        return ""

    @property
    def status(self):
        """ This device's status.

            For now, this should return a boolean:
                True    the device is open and ready for use
                False   the device is not open
        """
        return False

    @property
    def name(self):
        """ This device's name. """
        return self._name

    @property
    def isleaf(self):
        """ True if this device has no children. """
        return self.kids == 0

    @property
    def typeDescription(self):
        """ String describing the device type. """
        return self._type

    @property
    def type(self):
        """ Device type. """
        return self._type

    @property
    def packages(self):
        """ List of packages required to manage devices of this type.

            This list includes the packages required by its parent devices.
        """
        packages = self._packages
        for parent in self.parents:
            for package in parent.packages:
                if package not in packages:
                    packages.append(package)

        return packages

    @property
    def services(self):
        """ List of services required to manage devices of this type.

            This list includes the services required by its parent devices."
        """
        services = self._services
        for parent in self.parents:
            for service in parent.services:
                if service not in services:
                    services.append(service)

        return services

    @property
    def mediaPresent(self):
        return True


class NetworkStorageDevice(object):
    """ Virtual base class for network backed storage devices """

    def __init__(self, host_address=None, nic=None):
        """ Create a NetworkStorage Device instance. Note this class is only
            to be used as a baseclass and then only with multiple inheritance.
            The only correct use is:
            class MyStorageDevice(StorageDevice, NetworkStorageDevice):

            The sole purpose of this class is to:
            1) Be able to check if a StorageDevice is network backed
               (using isinstance).
            2) To be able to get the host address of the host (server) backing
               the storage *or* the NIC through which the storage is connected

            Arguments:

                host_address -- host address of the backing server
                nic -- nic to which the storage is bound
        """
        self.host_address = host_address
        self.nic = nic


class StorageDevice(Device):
    """ A generic storage device.

        A fully qualified path to the device node can be obtained via the
        path attribute, although it is not guaranteed to be useful, or
        even present, unless the StorageDevice's setup method has been
        run.

        StorageDevice instances can optionally contain a filesystem,
        represented by an FS instance. A StorageDevice's create method
        should create a filesystem if one has been specified.
    """
    _type = "storage device"
    _devDir = "/dev"
    sysfsBlockDir = "class/block"
    _resizable = False
    _partitionable = False
    _isDisk = False

    def __init__(self, name, format=None,
                 size=None, major=None, minor=None,
                 sysfsPath='', parents=None, exists=None, serial=None,
                 vendor="", model="", bus=""):
        """ Create a StorageDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                size -- the device's size (units/format TBD)
                major -- the device major
                minor -- the device minor
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                parents -- a list of required Device instances
                serial -- the ID_SERIAL_SHORT for this device
                vendor -- the manufacturer of this Device
                model -- manufacturer's device model string
                bus -- the interconnect this device uses

        """
        # allow specification of individual parents
        if isinstance(parents, Device):
            parents = [parents]

        self.exists = exists
        Device.__init__(self, name, parents=parents)

        self.uuid = None
        self._format = None
        self._size = numeric_type(size)
        self.major = numeric_type(major)
        self.minor = numeric_type(minor)
        self.sysfsPath = sysfsPath
        self._serial = serial
        self._vendor = vendor
        self._model = model
        self.bus = bus

        self.protected = False
        self.immutable = None
        self.controllable = True

        self.format = format
        self.originalFormat = self.format
        self.fstabComment = ""
        self._targetSize = self._size

        self._partedDevice = None

    @property
    def packages(self):
        """ List of packages required to manage devices of this type.

            This list includes the packages required by this device's
            format type as well those required by all of its parent 
            devices.
        """
        packages = super(StorageDevice, self).packages
        packages.extend(self.format.packages)
        for parent in self.parents:
            for package in parent.format.packages:
                if package not in packages:
                    packages.append(package)

        return packages

    @property
    def services(self):
        """ List of services required to manage devices of this type.

            This list includes the services required by this device's
            format type as well those required by all of its parent
            devices.
        """
        services = super(StorageDevice, self).services
        services.extend(self.format.services)
        for parent in self.parents:
            for service in parent.format.services:
                if service not in services:
                    services.append(service)

        return services

    @property
    def partedDevice(self):
        if self.exists and self.status and not self._partedDevice:
            log.debug("looking up parted Device: %s" % self.path)

            # We aren't guaranteed to be able to get a device.  In
            # particular, built-in USB flash readers show up as devices but
            # do not always have any media present, so parted won't be able
            # to find a device.
            try:
                self._partedDevice = parted.Device(path=self.path)
            except (_ped.IOException, _ped.DeviceException):
                pass

        return self._partedDevice

    def _getTargetSize(self):
        return self._targetSize

    def _setTargetSize(self, newsize):
        self._targetSize = newsize

    targetSize = property(lambda s: s._getTargetSize(),
                          lambda s, v: s._setTargetSize(v),
                          doc="Target size of this device")

    def __str__(self):
        s = Device.__str__(self)
        s += ("  uuid = %(uuid)s  format = %(format)r  size = %(size)s\n"
              "  major = %(major)s  minor = %(minor)r  exists = %(exists)s\n"
              "  sysfs path = %(sysfs)s  partedDevice = %(partedDevice)r\n"
              "  target size = %(targetSize)s  path = %(path)s\n"
              "  format args = %(formatArgs)s  originalFormat = %(origFmt)s" %
              {"uuid": self.uuid, "format": self.format, "size": self.size,
               "major": self.major, "minor": self.minor, "exists": self.exists,
               "sysfs": self.sysfsPath, "partedDevice": self.partedDevice,
               "targetSize": self.targetSize, "path": self.path,
               "formatArgs": self.formatArgs, "origFmt": self.originalFormat})
        return s

    @property
    def dict(self):
        d =  super(StorageDevice, self).dict
        d.update({"uuid": self.uuid, "size": self.size,
                  "format": self.format.dict, "removable": self.removable,
                  "major": self.major, "minor": self.minor,
                  "exists": self.exists, "sysfs": self.sysfsPath,
                  "targetSize": self.targetSize, "path": self.path})
        return d

    @property
    def path(self):
        """ Device node representing this device. """
        return "%s/%s" % (self._devDir, self.name)

    def updateSysfsPath(self):
        """ Update this device's sysfs path. """
        log_method_call(self, self.name, status=self.status)
        sysfsName = self.name.replace("/", "!")
        path = os.path.join("/sys", self.sysfsBlockDir, sysfsName)
        self.sysfsPath = os.path.realpath(path)[4:]
        log.debug("%s sysfsPath set to %s" % (self.name, self.sysfsPath))

    @property
    def formatArgs(self):
        """ Device-specific arguments to format creation program. """
        return []

    @property
    def resizable(self):
        """ Can this type of device be resized? """
        return (self._resizable and self.exists and
                (self.format.resizable or not self.format.type))

    def notifyKernel(self):
        """ Send a 'change' uevent to the kernel for this device. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            log.debug("not sending change uevent for non-existent device")
            return

        if not self.status:
            log.debug("not sending change uevent for inactive device")
            return

        path = os.path.normpath("/sys/%s" % self.sysfsPath)
        try:
            notify_kernel(path, action="change")
        except Exception, e:
            log.warning("failed to notify kernel of change: %s" % e)

    @property
    def fstabSpec(self):
        spec = self.path
        if self.format and self.format.uuid:
            spec = "UUID=%s" % self.format.uuid
        return spec

    def resize(self, intf=None):
        """ Resize the device.

            New size should already be set.
        """
        raise NotImplementedError("resize method not defined for StorageDevice")

    #
    # progress/status ui
    #
    # We define methods to create a wait window or a pulse progress window.
    # Device classes can define a _statusWindow method that calls the
    # appropriate method for display during device operations. Currently,
    # the only thing we display progress/status windows for is device creation.
    # MD and LVM devices can pass pulse progress windows down into the
    # devicelibs functions, but all other devices display a wait window.
    #
    def _progressWindow(self, intf=None, title="", msg=""):
        w = None
        if intf:
            w = intf.progressWindow(title, msg, 100, pulse=True)
        return w

    def _waitWindow(self, intf=None, title="", msg=""):
        w = None
        if intf:
            w = intf.waitWindow(title, msg)
        return w

    def _statusWindow(self, intf=None, title="", msg=""):
        return self._waitWindow(intf=intf, title=title, msg=msg)

    #
    # setup
    #
    def _preSetup(self, orig=False):
        """ Preparation and pre-condition checking for device setup.

            Return True if setup should proceed or False if not.
        """
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        if self.status or not self.controllable:
            return False

        self.setupParents(orig=orig)
        return True

    def _setup(self, intf=None, orig=False):
        """ Perform device-specific setup operations. """
        pass

    def setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        if not self._preSetup(orig=orig):
            return

        self._setup(intf=intf, orig=orig)
        self._postSetup()

    def _postSetup(self):
        """ Perform post-setup operations. """
        udev_settle()
        # we always probe since the device may not be set up when we want
        # information about it
        self._size = self.currentSize

    #
    # teardown
    #
    def _preTeardown(self, recursive=None):
        """ Preparation and pre-condition checking for device teardown.

            Return True if teardown should proceed or False if not.
        """
        if not self.exists and not recursive:
            raise DeviceError("device has not been created", self.name)

        if not self.status or not self.controllable:
            return False

        if self.originalFormat.exists:
            self.originalFormat.teardown()
        self.format.cacheMajorminor()
        if self.format.exists:
            self.format.teardown()
        udev_settle()
        return True

    def _teardown(self, recursive=None):
        """ Perform device-specific teardown operations. """
        pass

    def teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        if not self._preTeardown(recursive=recursive):
            return

        self._teardown(recursive=recursive)
        self._postTeardown(recursive=recursive)

    def _postTeardown(self, recursive=None):
        """ Perform post-teardown operations. """
        if recursive:
            self.teardownParents(recursive=recursive)

    #
    # create
    #
    def _preCreate(self):
        """ Preparation and pre-condition checking for device creation. """
        if self.exists:
            raise DeviceError("device has already been created", self.name)

        self.setupParents()

    def _create(self, w):
        """ Perform device-specific create operations. """
        pass

    def create(self, intf=None):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        self._preCreate()
        w = self._statusWindow(intf=intf,
                               title=_("Creating"),
                               msg=_("Creating device %s") % self.path)
        try:
            self._create(w)
        except Exception as e:
            raise DeviceCreateError(str(e), self.name)
        else:
            self._postCreate()
        finally:
            if w:
                w.pop()

    def _postCreate(self):
        """ Perform post-create operations. """
        self.exists = True
        self.setup()
        self.updateSysfsPath()
        udev_settle()

    #
    # destroy
    #
    def _preDestroy(self):
        """ Preparation and precondition checking for device destruction. """
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        if not self.isleaf:
            raise DeviceError("Cannot destroy non-leaf device", self.name)

        self.teardown()

    def _destroy(self):
        """ Perform device-specific destruction operations. """
        pass

    def destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        self._preDestroy()
        self._destroy()
        self._postDestroy()

    def _postDestroy(self):
        """ Perform post-destruction operations. """
        self.exists = False

    def setupParents(self, orig=False):
        """ Run setup method of all parent devices. """
        log_method_call(self, name=self.name, orig=orig, kids=self.kids)
        for parent in self.parents:
            parent.setup(orig=orig)
            if orig:
                _format = parent.originalFormat
            else:
                _format = parent.format

            # set up the formatting, if present
            if _format.type and _format.exists:
                _format.setup()

    def _getSize(self):
        """ Get the device's size in MB, accounting for pending changes. """
        if self.exists and not self.mediaPresent:
            return 0

        if self.exists and self.partedDevice:
            self._size = self.currentSize

        size = self._size
        if self.exists and self.resizable and self.targetSize != size:
            size = self.targetSize

        return size

    def _setSize(self, newsize):
        """ Set the device's size to a new value. """
        if newsize > self.maxSize:
            raise DeviceError("device cannot be larger than %s MB" %
                              (self.maxSize(),), self.name)
        self._size = newsize

    size = property(lambda x: x._getSize(),
                    lambda x, y: x._setSize(y),
                    doc="The device's size in MB, accounting for pending changes")

    @property
    def currentSize(self):
        """ The device's actual size. """
        size = 0
        if self.exists and self.partedDevice:
            size = self.partedDevice.getSize()
        elif self.exists:
            size = self._size
        return size

    @property
    def minSize(self):
        """ The minimum size this device can be. """
        if self.format.minSize:
            return self.format.minSize
        else:
            return self.size

    @property
    def maxSize(self):
        """ The maximum size this device can be. """
        if self.format.maxSize > self.currentSize:
            return self.currentSize
        else:
            return self.format.maxSize

    @property
    def status(self):
        """ This device's status.

            For now, this should return a boolean:
                True    the device is open and ready for use
                False   the device is not open
        """
        if not self.exists:
            return False
        return os.access(self.path, os.W_OK)

    def _setFormat(self, format):
        """ Set the Device's format. """
        if not format:
            format = getFormat(None, device=self.path, exists=self.exists)
        log_method_call(self, self.name, type=format.type,
                        current=getattr(self._format, "type", None))
        if self._format and self._format.status:
            # FIXME: self.format.status doesn't mean much
            raise DeviceError("cannot replace active format", self.name)

        self._format = format

    def _getFormat(self):
        return self._format

    format = property(lambda d: d._getFormat(),
                      lambda d,f: d._setFormat(f),
                      doc="The device's formatting.")

    def preCommitFixup(self, *args, **kwargs):
        """ Do any necessary pre-commit fixups."""
        pass

    @property
    def removable(self):
        devpath = os.path.normpath("/sys/%s" % self.sysfsPath)
        remfile = os.path.normpath("%s/removable" % devpath)
        return (self.sysfsPath and os.path.exists(devpath) and
                os.access(remfile, os.R_OK) and
                open(remfile).readline().strip() == "1")

    @property
    def isDisk(self):
        return self._isDisk

    @property
    def partitionable(self):
        return self._partitionable

    @property
    def partitioned(self):
        return self.format.type == "disklabel" and self.partitionable

    @property
    def serial(self):
        return self._serial

    @property
    def model(self):
        if not self._model:
            self._model = getattr(self.partedDevice, "model", "")
        return self._model

    @property
    def vendor(self):
        return self._vendor

    @property
    def growable(self):
        """ True if this device or it's component devices are growable. """
        grow = getattr(self, "req_grow", False)
        if not grow:
            for parent in self.parents:
                grow = parent.growable
                if grow:
                    break
        return grow


class DiskDevice(StorageDevice):
    """ A disk """
    _type = "disk"
    _partitionable = True
    _isDisk = True

    def __init__(self, name, format=None,
                 size=None, major=None, minor=None, sysfsPath='',
                 parents=None, serial=None, vendor="", model="", bus="",
                 exists=True):
        """ Create a DiskDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                size -- the device's size (units/format TBD)
                major -- the device major
                minor -- the device minor
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                parents -- a list of required Device instances
                removable -- whether or not this is a removable device
                serial -- the ID_SERIAL_SHORT for this device
                vendor -- the manufacturer of this Device
                model -- manufacturer's device model string
                bus -- the interconnect this device uses


            DiskDevices always exist.
        """
        StorageDevice.__init__(self, name, format=format, size=size,
                               major=major, minor=minor, exists=exists,
                               sysfsPath=sysfsPath, parents=parents,
                               serial=serial, model=model,
                               vendor=vendor, bus=bus)

    def __str__(self):
        s = StorageDevice.__str__(self)
        s += ("  removable = %(removable)s  partedDevice = %(partedDevice)r" %
              {"removable": self.removable, "partedDevice": self.partedDevice})
        return s

    @property
    def mediaPresent(self):
        if not self.partedDevice:
            return False

        # Some drivers (cpqarray <blegh>) make block device nodes for
        # controllers with no disks attached and then report a 0 size,
        # treat this as no media present
        return self.partedDevice.getSize() != 0

    @property
    def description(self):
        return self.model

    @property
    def size(self):
        """ The disk's size in MB """
        return super(DiskDevice, self).size
    #size = property(StorageDevice._getSize)

    def _preDestroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        if not self.mediaPresent:
            raise DeviceError("cannot destroy disk with no media", self.name)

        StorageDevice._preDestroy(self)


class PartitionDevice(StorageDevice):
    """ A disk partition.

        On types and flags...

        We don't need to deal with numerical partition types at all. The
        only type we are concerned with is primary/logical/extended. Usage
        specification is accomplished through the use of flags, which we
        will set according to the partition's format.
    """
    _type = "partition"
    _resizable = True
    defaultSize = 500

    def __init__(self, name, format=None,
                 size=None, grow=False, maxsize=None,
                 major=None, minor=None, bootable=None,
                 sysfsPath='', parents=None, exists=None,
                 partType=None, primary=False, weight=0):
        """ Create a PartitionDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                exists -- indicates whether this is an existing device
                format -- the device's format (DeviceFormat instance)

                For existing partitions:

                    parents -- the disk that contains this partition
                    major -- the device major
                    minor -- the device minor
                    sysfsPath -- sysfs device path

                For new partitions:

                    partType -- primary,extended,&c (as parted constant)
                    grow -- whether or not to grow the partition
                    maxsize -- max size for growable partitions (in MB)
                    size -- the device's size (in MB)
                    bootable -- whether the partition is bootable
                    parents -- a list of potential containing disks
                    weight -- an initial sorting weight to assign
        """
        self.req_disks = []
        self.req_partType = None
        self.req_primary = None
        self.req_grow = None
        self.req_bootable = None
        self.req_size = 0
        self.req_base_size = 0
        self.req_max_size = 0
        self.req_base_weight = 0

        self._bootable = False

        StorageDevice.__init__(self, name, format=format, size=size,
                               major=major, minor=minor, exists=exists,
                               sysfsPath=sysfsPath, parents=parents)

        if not exists:
            # this is a request, not a partition -- it has no parents
            self.req_disks = self.parents[:]
            for dev in self.parents:
                dev.removeChild()
            self.parents = []

        # FIXME: Validate partType, but only if this is a new partition
        #        Otherwise, overwrite it with the partition's type.
        self._partType = None
        self.partedFlags = {}
        self._partedPartition = None
        self._origPath = None
        self._currentSize = 0

        # FIXME: Validate size, but only if this is a new partition.
        #        For existing partitions we will get the size from
        #        parted.

        if self.exists:
            log.debug("looking up parted Partition: %s" % self.path)
            self._partedPartition = self.disk.format.partedDisk.getPartitionByPath(self.path)
            if not self._partedPartition:
                raise DeviceError("cannot find parted partition instance", self.name)

            self._origPath = self.path
            # collect information about the partition from parted
            self.probe()
            if self.getFlag(parted.PARTITION_PREP):
                # the only way to identify a PPC PReP Boot partition is to
                # check the partition type/flags, so do it here.
                self.format = getFormat("prepboot", device=self.path, exists=True)
        else:
            # XXX It might be worthwhile to create a shit-simple
            #     PartitionRequest class and pass one to this constructor
            #     for new partitions.
            if not self._size:
                # default size for new partition requests
                self._size = self.defaultSize
            self.req_name = name
            self.req_partType = partType
            self.req_primary = primary
            self.req_max_size = numeric_type(maxsize)
            self.req_grow = grow
            self.req_bootable = bootable

            # req_size may be manipulated in the course of partitioning
            self.req_size = self._size

            # req_base_size will always remain constant
            self.req_base_size = self._size

            self.req_base_weight = weight

    def __str__(self):
        s = StorageDevice.__str__(self)
        s += ("  grow = %(grow)s  max size = %(maxsize)s  bootable = %(bootable)s\n"
              "  part type = %(partType)s  primary = %(primary)s\n"
              "  partedPartition = %(partedPart)r  disk = %(disk)r\n" %
              {"grow": self.req_grow, "maxsize": self.req_max_size,
               "bootable": self.bootable, "partType": self.partType,
               "primary": self.req_primary,
               "partedPart": self.partedPartition, "disk": self.disk})

        if self.partedPartition:
            s += ("  start = %(start)s  end = %(end)s  length = %(length)s\n"
                  "  flags = %(flags)s" %
                  {"length": self.partedPartition.geometry.length,
                   "start": self.partedPartition.geometry.start,
                   "end": self.partedPartition.geometry.end,
                   "flags": self.partedPartition.getFlagsAsString()})

        return s

    @property
    def dict(self):
        d = super(PartitionDevice, self).dict
        d.update({"type": self.partType})
        if not self.exists:
            d.update({"grow": self.req_grow, "maxsize": self.req_max_size,
                      "bootable": self.bootable,
                      "primary": self.req_primary})

        if self.partedPartition:
            d.update({"length": self.partedPartition.geometry.length,
                      "start": self.partedPartition.geometry.start,
                      "end": self.partedPartition.geometry.end,
                      "flags": self.partedPartition.getFlagsAsString()})
        return d

    def writeKS(self, f, preexisting=False, noformat=False, s=None):
        args = []

        if self.isExtended:
            return

        if self.req_grow:
            args.append("--grow")
        if self.req_max_size:
            args.append("--maxsize=%s" % self.req_max_size)
        if self.req_primary:
            args.append("--asprimary")
        if self.req_size:
            args.append("--size=%s" % (self.req_size or self.defaultSize))
        if preexisting:
            if len(self.req_disks) == 1:
                args.append("--ondisk=%s" % self.req_disks[0].name)
            else:
                args.append("--onpart=%s" % self.name)
        if noformat:
            args.append("--noformat")

        f.write("#part ")
        self.format.writeKS(f)
        f.write(" %s" % " ".join(args))
        if s:
            f.write(" %s" % s)

    def _setTargetSize(self, newsize):
        if newsize != self.currentSize:
            # change this partition's geometry in-memory so that other
            # partitioning operations can complete (e.g., autopart)
            self._targetSize = newsize
            disk = self.disk.format.partedDisk

            # resize the partition's geometry in memory
            (constraint, geometry) = self._computeResize(self.partedPartition)
            disk.setPartitionGeometry(partition=self.partedPartition,
                                      constraint=constraint,
                                      start=geometry.start, end=geometry.end)

    @property
    def path(self):
        if not self.parents:
            devDir = StorageDevice._devDir
        else:
            devDir = self.parents[0]._devDir

        return "%s/%s" % (devDir, self.name)

    @property
    def partType(self):
        """ Get the partition's type (as parted constant). """
        try:
            ptype = self.partedPartition.type
        except AttributeError:
            ptype = self._partType

        if not self.exists and ptype is None:
            ptype = self.req_partType

        return ptype

    @property
    def isExtended(self):
        return (self.partType is not None and
                self.partType & parted.PARTITION_EXTENDED)

    @property
    def isLogical(self):
        return (self.partType is not None and
                self.partType & parted.PARTITION_LOGICAL)

    @property
    def isPrimary(self):
        return (self.partType is not None and
                self.partType == parted.PARTITION_NORMAL)

    @property
    def isProtected(self):
        return (self.partType is not None and
                self.partType & parted.PARTITION_PROTECTED)

    @property
    def fstabSpec(self):
        spec = self.path
        if self.disk and self.disk.type == 'dasd':
            spec = deviceNameToDiskByPath(self.name)
        elif self.format and self.format.uuid:
            spec = "UUID=%s" % self.format.uuid
        return spec

    def _getPartedPartition(self):
        return self._partedPartition

    def _setPartedPartition(self, partition):
        """ Set this PartitionDevice's parted Partition instance. """
        log_method_call(self, self.name)
        if partition is None:
            path = None
        elif isinstance(partition, parted.Partition):
            path = partition.path
        else:
            raise ValueError("partition must be a parted.Partition instance")

        log.debug("device %s new partedPartition %s has path %s" % (self.name,
                                                                    partition,
                                                                    path))
        self._partedPartition = partition
        self.updateName()

    partedPartition = property(lambda d: d._getPartedPartition(),
                               lambda d,p: d._setPartedPartition(p))

    def preCommitFixup(self, *args, **kwargs):
        """ Re-get self.partedPartition from the original disklabel. """
        log_method_call(self, self.name)
        if not self.exists:
            return

        # find the correct partition on the original parted.Disk since the
        # name/number we're now using may no longer match
        _disklabel = self.disk.originalFormat

        if self.isExtended:
            # getPartitionBySector doesn't work on extended partitions
            _partition = _disklabel.extendedPartition
            log.debug("extended lookup found partition %s"
                        % devicePathToName(getattr(_partition, "path", None)))
        else:
            # lookup the partition by sector to avoid the renumbering
            # nonsense entirely
            _sector = self.partedPartition.geometry.start
            _partition = _disklabel.partedDisk.getPartitionBySector(_sector)
            log.debug("sector-based lookup found partition %s"
                        % devicePathToName(getattr(_partition, "path", None)))

        self.partedPartition = _partition

    def _getWeight(self):
        return self.req_base_weight

    def _setWeight(self, weight):
        self.req_base_weight = weight

    weight = property(lambda d: d._getWeight(),
                      lambda d,w: d._setWeight(w))

    def updateSysfsPath(self):
        """ Update this device's sysfs path. """
        log_method_call(self, self.name, status=self.status)
        if not self.parents:
            self.sysfsPath = ''

        elif isinstance(self.parents[0], DMDevice):
            dm_node = dm.dm_node_from_name(self.name)
            path = os.path.join("/sys", self.sysfsBlockDir, dm_node)
            self.sysfsPath = os.path.realpath(path)[4:]
        else:
            StorageDevice.updateSysfsPath(self)

    def updateName(self):
        if self.partedPartition is None:
            self._name = self.req_name
        else:
            self._name = \
                devicePathToName(self.partedPartition.getDeviceNodeName())

    def dependsOn(self, dep):
        """ Return True if this device depends on dep. """
        if isinstance(dep, PartitionDevice) and dep.isExtended and \
           self.isLogical and self.disk == dep.disk:
            return True

        return Device.dependsOn(self, dep)

    def _setFormat(self, format):
        """ Set the Device's format. """
        log_method_call(self, self.name)
        StorageDevice._setFormat(self, format)

    def _setBootable(self, bootable):
        """ Set the bootable flag for this partition. """
        if self.partedPartition:
            if iutil.isS390():
                return
            if self.flagAvailable(parted.PARTITION_BOOT):
                if bootable:
                    self.setFlag(parted.PARTITION_BOOT)
                else:
                    self.unsetFlag(parted.PARTITION_BOOT)
            else:
                raise DeviceError("boot flag not available for this partition", self.name)

            self._bootable = bootable
        else:
            self.req_bootable = bootable

    def _getBootable(self):
        return self._bootable or self.req_bootable

    bootable = property(_getBootable, _setBootable)

    def flagAvailable(self, flag):
        log_method_call(self, path=self.path, flag=flag)
        if not self.partedPartition:
            return

        return self.partedPartition.isFlagAvailable(flag)

    def getFlag(self, flag):
        log_method_call(self, path=self.path, flag=flag)
        if not self.partedPartition or not self.flagAvailable(flag):
            return

        return self.partedPartition.getFlag(flag)

    def setFlag(self, flag):
        log_method_call(self, path=self.path, flag=flag)
        if not self.partedPartition or not self.flagAvailable(flag):
            return

        self.partedPartition.setFlag(flag)

    def unsetFlag(self, flag):
        log_method_call(self, path=self.path, flag=flag)
        if not self.partedPartition or not self.flagAvailable(flag):
            return

        self.partedPartition.unsetFlag(flag)

    def probe(self):
        """ Probe for any missing information about this device.

            size, partition type, flags
        """
        log_method_call(self, self.name, exists=self.exists)
        if not self.exists:
            return

        # this is in MB
        self._size = self.partedPartition.getSize()
        self._currentSize = self._size
        self.targetSize = self._size

        self._partType = self.partedPartition.type

        self._bootable = self.getFlag(parted.PARTITION_BOOT)

    def _create(self, w):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        self.disk.format.addPartition(self.partedPartition)

        try:
            self.disk.format.commit()
        except DiskLabelCommitError:
            part = self.disk.format.partedDisk.getPartitionByPath(self.path)
            self.disk.format.removePartition(part)
            raise

    def _postCreate(self):
        if not self.isExtended:
            # Ensure old metadata which lived in freespace so did not get
            # explictly destroyed by a destroyformat action gets wiped
            DeviceFormat(device=self.path, exists=True).destroy()

        self.partedPartition = self.disk.format.partedDisk.getPartitionByPath(self.path)
        StorageDevice._postCreate(self)
        self._currentSize = self.partedPartition.getSize()

    def _computeResize(self, partition):
        log_method_call(self, self.name, status=self.status)

        # compute new size for partition
        currentGeom = partition.geometry
        currentDev = currentGeom.device
        newLen = long(self.targetSize * 1024 * 1024) / currentDev.sectorSize
        newGeometry = parted.Geometry(device=currentDev,
                                      start=currentGeom.start,
                                      length=newLen)
        # and align the end sector
        newGeometry.end = self.disk.format.endAlignment.alignDown(newGeometry,
                                                               newGeometry.end)
        constraint = parted.Constraint(exactGeom=newGeometry)

        return (constraint, newGeometry)

    def resize(self, intf=None):
        """ Resize the device.

            self.targetSize must be set to the new size.
        """
        log_method_call(self, self.name, status=self.status)
        self._preDestroy()
        if self.targetSize != self.currentSize:
            # partedDisk has been restored to _origPartedDisk, so
            # recalculate resize geometry because we may have new
            # partitions on the disk, which could change constraints
            partedDisk = self.disk.format.partedDisk
            partition = partedDisk.getPartitionByPath(self.path)
            (constraint, geometry) = self._computeResize(partition)

            partedDisk.setPartitionGeometry(partition=partition,
                                            constraint=constraint,
                                            start=geometry.start,
                                            end=geometry.end)

            self.disk.format.commit()
            self._currentSize = partition.getSize()

    def _preDestroy(self):
        StorageDevice._preDestroy(self)
        if not self.sysfsPath:
            return

        self.setupParents(orig=True)

    def _destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        # we should have already set self.partedPartition to point to the
        # partition on the original disklabel
        self.disk.originalFormat.removePartition(self.partedPartition)
        try:
            self.disk.originalFormat.commit()
        except DiskLabelCommitError:
            self.disk.originalFormat.addPartition(self.partedPartition)
            self.partedPartition = self.disk.originalFormat.partedDisk.getPartitionByPath(self.path)
            raise

    def deactivate(self):
        """
        This is never called. For instructional purposes only.

        We do not want multipath partitions disappearing upon their teardown().
        """
        if self.parents[0].type == 'dm-multipath':
            devmap = block.getMap(major=self.major, minor=self.minor)
            if devmap:
                try:
                    block.removeDeviceMap(devmap)
                except Exception as e:
                    raise DeviceTeardownError("failed to tear down device-mapper partition %s: %s" % (self.name, e))
            udev_settle()

    def _getSize(self):
        """ Get the device's size. """
        size = self._size
        if self.partedPartition:
            # this defaults to MB
            size = self.partedPartition.getSize()
        return size

    def _setSize(self, newsize):
        """ Set the device's size (for resize, not creation).

            Arguments:

                newsize -- the new size (in MB)

        """
        log_method_call(self, self.name,
                        status=self.status, size=self._size, newsize=newsize)
        if not self.exists:
            raise DeviceError("device does not exist", self.name)

        if newsize > self.disk.size:
            raise ValueError("partition size would exceed disk size")

        # this defaults to MB
        maxAvailableSize = self.partedPartition.getMaxAvailableSize()

        if newsize > maxAvailableSize:
            raise ValueError("new size is greater than available space")

         # now convert the size to sectors and update the geometry
        geometry = self.partedPartition.geometry
        physicalSectorSize = geometry.device.physicalSectorSize

        new_length = (newsize * (1024 * 1024)) / physicalSectorSize
        geometry.length = new_length

    def _getDisk(self):
        """ The disk that contains this partition."""
        try:
            disk = self.parents[0]
        except IndexError:
            disk = None
        return disk

    def _setDisk(self, disk):
        """Change the parent.

        Setting up a disk is not trivial.  It has the potential to change
        the underlying object.  If necessary we must also change this object.
        """
        log_method_call(self, self.name, old=getattr(self.disk, "name", None),
                        new=getattr(disk, "name", None))
        if self.disk:
            self.disk.removeChild()

        if disk:
            self.parents = [disk]
            disk.addChild()
        else:
            self.parents = []

    disk = property(lambda p: p._getDisk(), lambda p,d: p._setDisk(d))

    @property
    def maxSize(self):
        """ The maximum size this partition can be. """
        # XXX: this is MB by default
        maxPartSize = self.partedPartition.getMaxAvailableSize()

        if self.format.maxSize > maxPartSize:
            return maxPartSize
        else:
            return self.format.maxSize

    @property
    def currentSize(self):
        """ The device's actual size. """
        if self.exists:
            return self._currentSize
        else:
            return 0

    @property
    def resizable(self):
        """ Can this type of device be resized? """
        return super(PartitionDevice, self).resizable and \
               self.disk.type != 'dasd'

class DMDevice(StorageDevice):
    """ A device-mapper device """
    _type = "dm"
    _devDir = "/dev/mapper"

    def __init__(self, name, format=None, size=None, dmUuid=None,
                 target=None, exists=None, parents=None, sysfsPath=''):
        """ Create a DMDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                target -- the device-mapper target type (string)
                size -- the device's size (units/format TBD)
                dmUuid -- the device's device-mapper UUID
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                parents -- a list of required Device instances
                exists -- indicates whether this is an existing device
        """
        StorageDevice.__init__(self, name, format=format, size=size,
                               exists=exists,
                               parents=parents, sysfsPath=sysfsPath)
        self.target = target
        self.dmUuid = dmUuid

    def __str__(self):
        s = StorageDevice.__str__(self)
        s += ("  target = %(target)s  dmUuid = %(dmUuid)s" %
              {"target": self.target, "dmUuid": self.dmUuid})
        return s

    @property
    def dict(self):
        d = super(DMDevice, self).dict
        d.update({"target": self.target, "dmUuid": self.dmUuid})
        return d

    @property
    def fstabSpec(self):
        """ Return the device specifier for use in /etc/fstab. """
        return self.path

    @property
    def mapName(self):
        """ This device's device-mapper map name """
        return self.name

    @property
    def status(self):
        _status = False
        for map in block.dm.maps():
            if map.name == self.mapName:
                _status = map.live_table and not map.suspended
                break

        return _status

    def updateSysfsPath(self):
        """ Update this device's sysfs path. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        if self.status:
            dm_node = self.getDMNode()
            path = os.path.join("/sys", self.sysfsBlockDir, dm_node)
            self.sysfsPath = os.path.realpath(path)[4:]
        else:
            self.sysfsPath = ''

    #def getTargetType(self):
    #    return dm.getDmTarget(name=self.name)

    def getDMNode(self):
        """ Return the dm-X (eg: dm-0) device node for this device. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        return dm.dm_node_from_name(self.name)

    def setupPartitions(self):
        log_method_call(self, name=self.name, kids=self.kids)
        rc = iutil.execWithRedirect("kpartx",
                                ["-a", "-p", "p", self.path],
                                stdout = "/dev/tty5",
                                stderr = "/dev/tty5")
        if rc:
            raise DMError("partition activation failed for '%s'" % self.name)
        udev_settle()

    def teardownPartitions(self):
        log_method_call(self, name=self.name, kids=self.kids)
        rc = iutil.execWithRedirect("kpartx",
                                ["-d", "-p", "p", self.path],
                                stdout = "/dev/tty5",
                                stderr = "/dev/tty5")
        if rc:
            raise DMError("partition deactivation failed for '%s'" % self.name)
        udev_settle()

    def _setName(self, name):
        """ Set the device's map name. """
        log_method_call(self, self.name, status=self.status)
        if self.status:
            raise DeviceError("cannot rename active device", self.name)

        self._name = name
        #self.sysfsPath = "/dev/disk/by-id/dm-name-%s" % self.name

    name = property(lambda d: d._name,
                    lambda d,n: d._setName(n))

    @property
    def slave(self):
        """ This device's backing device. """
        return self.parents[0]


class DMLinearDevice(DMDevice):
    _type = "dm-linear"
    _partitionable = True
    _isDisk = True

    def __init__(self, name, format=None, size=None, dmUuid=None,
                 exists=None, parents=None, sysfsPath=''):
        """ Create a DMLinearDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                size -- the device's size (units/format TBD)
                dmUuid -- the device's device-mapper UUID
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                parents -- a list of required Device instances
                exists -- indicates whether this is an existing device
        """
        if not parents:
            raise ValueError("DMLinearDevice requires a backing block device")

        DMDevice.__init__(self, name, format=format, size=size,
                          parents=parents, sysfsPath=sysfsPath,
                          exists=exists, target="linear", dmUuid=dmUuid)

    def _setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        slave_length = self.slave.partedDevice.length
        dm.dm_create_linear(self.name, self.slave.path, slave_length,
                            self.dmUuid)

    def _postSetup(self):
        StorageDevice._postSetup(self)
        self.setupPartitions()
        udev_settle()

    def _teardown(self, recursive=False):
        self.teardownPartitions()
        udev_settle()
        dm.dm_remove(self.name)
        udev_settle()

    def deactivate(self, recursive=False):
        StorageDevice.teardown(self)

    def teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        if not self._preTeardown(recursive=recursive):
            return

        log.debug("not tearing down dm-linear device %s" % self.name)

    @property
    def description(self):
        return self.model


class DMCryptDevice(DMDevice):
    """ A dm-crypt device """
    _type = "dm-crypt"

    def __init__(self, name, format=None, size=None, uuid=None,
                 exists=None, sysfsPath='', parents=None):
        """ Create a DMCryptDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                size -- the device's size (units/format TBD)
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                parents -- a list of required Device instances
                exists -- indicates whether this is an existing device
        """
        DMDevice.__init__(self, name, format=format, size=size,
                          parents=parents, sysfsPath=sysfsPath,
                          exists=exists, target="crypt")

class LUKSDevice(DMCryptDevice):
    """ A mapped LUKS device. """
    _type = "luks/dm-crypt"
    _packages = ["cryptsetup-luks"]

    def __init__(self, name, format=None, size=None, uuid=None,
                 exists=None, sysfsPath='', parents=None):
        """ Create a LUKSDevice instance.

            Arguments:

                name -- the device name

            Keyword Arguments:

                size -- the device's size in MB
                uuid -- the device's UUID
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                parents -- a list of required Device instances
                exists -- indicates whether this is an existing device
        """
        DMCryptDevice.__init__(self, name, format=format, size=size,
                               parents=parents, sysfsPath=sysfsPath,
                               uuid=None, exists=exists)

    def writeKS(self, f, preexisting=False, noformat=False, s=None):
        self.slave.writeKS(f, preexisting=preexisting, noformat=noformat, s=s)
        f.write(" ")
        self.format.writeKS(f)
        if s:
            f.write(" %s" % s)

    @property
    def size(self):
        if not self.exists or not self.partedDevice:
            # the LUKS metadata area is 2MB
            size = float(self.slave.size) - 2.0
        else:
            size = self.partedDevice.getSize()
        return size

    def _postCreate(self):
        self._name = self.slave.format.mapName
        StorageDevice._postCreate(self)

    def _postTeardown(self, recursive=False):
        if not recursive:
            # this is handled by StorageDevice._postTeardown if recursive
            # is True
            self.teardownParents(recursive=recursive)

        StorageDevice._postTeardown(self, recursive=recursive)

    def dracutSetupString(self):
        return "rd_LUKS_UUID=luks-%s" % self.slave.format.uuid


class LVMVolumeGroupDevice(DMDevice):
    """ An LVM Volume Group

        XXX Maybe this should inherit from StorageDevice instead of
            DMDevice since there's no actual device.
    """
    _type = "lvmvg"
    _packages = ["lvm2"]

    def __init__(self, name, parents, size=None, free=None,
                 peSize=None, peCount=None, peFree=None, pvCount=None,
                 uuid=None, exists=None, sysfsPath=''):
        """ Create a LVMVolumeGroupDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)
                parents -- a list of physical volumes (StorageDevice)

            Keyword Arguments:

                peSize -- physical extent size (in MB)
                exists -- indicates whether this is an existing device
                sysfsPath -- sysfs device path

                For existing VG's only:

                    size -- the VG's size (in MB)
                    free -- amount of free space in the VG
                    peFree -- number of free extents
                    peCount -- total number of extents
                    pvCount -- number of PVs in this VG
                    uuid -- the VG's UUID

        """
        self.pvClass = get_device_format_class("lvmpv")
        if not self.pvClass:
            raise StorageError("cannot find 'lvmpv' class")

        if isinstance(parents, list):
            for dev in parents:
                if not isinstance(dev.format, self.pvClass):
                    raise ValueError("constructor requires a list of PVs")
        elif not isinstance(parents.format, self.pvClass):
            raise ValueError("constructor requires a list of PVs")

        DMDevice.__init__(self, name, parents=parents,
                          exists=exists, sysfsPath=sysfsPath)

        self.uuid = uuid
        self.free = numeric_type(free)
        self.peSize = numeric_type(peSize)
        self.peCount = numeric_type(peCount)
        self.peFree = numeric_type(peFree)
        self.pvCount = numeric_type(pvCount)
        self.lv_names = []
        self.lv_uuids = []
        self.lv_sizes = []
        self.lv_attr = []
        self.hasDuplicate = False

        # circular references, here I come
        self._lvs = []

        # TODO: validate peSize if given
        if not self.peSize:
            self.peSize = 32.0   # MB

        if not self.exists:
            self.pvCount = len(self.parents)

        # Some snapshots don't have a proper LV as an origin (--vorigin).
        # They still occupy space in the VG.
        self.voriginSnapshots = {}

    def __str__(self):
        s = DMDevice.__str__(self)
        s += ("  free = %(free)s  PE Size = %(peSize)s  PE Count = %(peCount)s\n"
              "  PE Free = %(peFree)s  PV Count = %(pvCount)s\n"
              "  LV Names = %(lv_names)s  modified = %(modified)s\n"
              "  extents = %(extents)s  free space = %(freeSpace)s\n"
              "  free extents = %(freeExtents)s\n"
              "  PVs = %(pvs)s\n"
              "  LVs = %(lvs)s" %
              {"free": self.free, "peSize": self.peSize, "peCount": self.peCount,
               "peFree": self.peFree, "pvCount": self.pvCount,
               "lv_names": self.lv_names, "modified": self.isModified,
               "extents": self.extents, "freeSpace": self.freeSpace,
               "freeExtents": self.freeExtents, "pvs": self.pvs, "lvs": self.lvs})
        return s

    @property
    def dict(self):
        d = super(LVMVolumeGroupDevice, self).dict
        d.update({"free": self.free, "peSize": self.peSize,
                  "peCount": self.peCount, "peFree": self.peFree,
                  "pvCount": self.pvCount, "extents": self.extents,
                  "freeSpace": self.freeSpace,
                  "freeExtents": self.freeExtents,
                  "lv_names": self.lv_names,
                  "lv_uuids": self.lv_uuids,
                  "lv_sizes": self.lv_sizes,
                  "lv_attr": self.lv_attr,
                  "lvNames": [lv.name for lv in self.lvs]})
        return d

    def writeKS(self, f, preexisting=False, noformat=False, s=None):
        args = ["--pesize=%s" % int(self.peSize * 1024)]
        pvs = []

        for pv in self.pvs:
            pvs.append("pv.%s" % pv.format.majorminor)

        if preexisting:
            args.append("--useexisting")
        if noformat:
            args.append("--noformat")

        f.write("#volgroup %s %s %s" % (self.name, " ".join(args), " ".join(pvs)))
        if s:
            f.write(" %s" % s)

    @property
    def mapName(self):
        """ This device's device-mapper map name """
        # Thank you lvm for this lovely hack.
        return self.name.replace("-","--")

    @property
    def path(self):
        """ Device node representing this device. """
        return "%s/%s" % (self._devDir, self.mapName)

    def updateSysfsPath(self):
        """ Update this device's sysfs path. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        self.sysfsPath = ''

    @property
    def status(self):
        """ The device's status (True means active). """
        if not self.exists:
            return False

        # certainly if any of this VG's LVs are active then so are we
        for lv in self.lvs:
            if lv.status:
                return True

        # if any of our PVs are not active then we cannot be
        for pv in self.pvs:
            if not pv.status:
                return False

        # if we are missing some of our PVs we cannot be active
        if not self.complete:
            return False

        return True

    def _addDevice(self, device):
        """ Add a new physical volume device to the volume group.

            XXX This is for use by device probing routines and is not
                intended for modification of the VG.
        """
        log_method_call(self,
                        self.name,
                        device=device.name,
                        status=self.status)
        if not self.exists:
            raise DeviceError("device does not exist", self.name)

        if not isinstance(device.format, self.pvClass):
            raise ValueError("addDevice requires a PV arg")

        if self.uuid and device.format.vgUuid != self.uuid:
            # this means there is another vg with the same name on the system
            # set hasDuplicate which will make complete return False
            # and let devicetree._handleInconsistencies() further handle this.
            # Note we still add the device to our parents for use by
            # devicetree._handleInconsistencies()
            self.hasDuplicate = True

        if device in self.pvs:
            raise ValueError("device is already a member of this VG")

        self.parents.append(device)
        device.addChild()

        # now see if the VG can be activated
        if self.complete:
            self.setup()

    def _removeDevice(self, device):
        """ Remove a physical volume from the volume group.

            This is for cases like clearing of preexisting partitions.
        """
        log_method_call(self,
                        self.name,
                        device=device.name,
                        status=self.status)
        try:
            self.parents.remove(device)
        except ValueError, e:
            raise ValueError("cannot remove non-member PV device from VG")

        device.removeChild()

    def _preSetup(self, orig=False):
        if self.exists and not self.complete:
            raise DeviceError("cannot activate VG with missing PV(s)", self.name)
        return StorageDevice._preSetup(self, orig=orig)

    def _teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        lvm.vgdeactivate(self.name)

    def _statusWindow(self, intf=None, title="", msg=""):
        return self._progressWindow(intf=intf, title=title, msg=msg)

    def _create(self, w):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        pv_list = [pv.path for pv in self.parents]
        lvm.vgcreate(self.name, pv_list, self.peSize, progress=w)

    def _preDestroy(self):
        StorageDevice._preDestroy(self)
        # set up the pvs since lvm needs access to them to do the vgremove
        self.setupParents(orig=True)

    def _destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        lvm.vgreduce(self.name, [], rm=True)
        lvm.vgremove(self.name)

    def reduce(self, pv_list):
        """ Remove the listed PVs from the VG. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        lvm.vgreduce(self.name, pv_list)
        # XXX do we need to notify the kernel?

    def _addLogVol(self, lv):
        """ Add an LV to this VG. """
        if lv in self._lvs:
            raise ValueError("lv is already part of this vg")

        # verify we have the space, then add it
        # do not verify for growing vg (because of ks)
        if not lv.exists and not self.growable and lv.size > self.freeSpace:
            raise DeviceError("new lv is too large to fit in free space", self.name)

        log.debug("Adding %s/%dMB to %s" % (lv.name, lv.size, self.name))
        self._lvs.append(lv)

    def _removeLogVol(self, lv):
        """ Remove an LV from this VG. """
        if lv not in self.lvs:
            raise ValueError("specified lv is not part of this vg")

        self._lvs.remove(lv)

    def _addPV(self, pv):
        """ Add a PV to this VG. """
        if pv in self.pvs:
            raise ValueError("pv is already part of this vg")

        # for the time being we will not allow vgextend
        if self.exists:
            raise DeviceError("cannot add pv to existing vg", self.name)

        self.parents.append(pv)
        pv.addChild()

        # and update our pv count
        self.pvCount = len(self.parents)

    def _removePV(self, pv):
        """ Remove an PV from this VG. """
        if not pv in self.pvs:
            raise ValueError("specified pv is not part of this vg")

        # for the time being we will not allow vgreduce
        if self.exists:
            raise DeviceError("cannot remove pv from existing vg", self.name)

        self.parents.remove(pv)
        pv.removeChild()

        # and update our pv count
        self.pvCount = len(self.parents)

    # We can't rely on lvm to tell us about our size, free space, &c
    # since we could have modifications queued, unless the VG and all of
    # its PVs already exist.
    #
    #        -- liblvm may contain support for in-memory devices

    @property
    def isModified(self):
        """ Return True if the VG has changes queued that LVM is unaware of. """
        modified = True
        if self.exists and not filter(lambda d: not d.exists, self.pvs):
            modified = False

        return modified

    @property
    def snapshotSpace(self):
        """ Total space used by snapshots in this volume group. """
        used = 0
        for lv in self.lvs:
            log.debug("lv %s uses %dMB for snapshots" % (lv.lvname,
                                                         lv.snapshotSpace))
            used += self.align(lv.snapshotSpace, roundup=True)

        for (vname, vsize) in self.voriginSnapshots.items():
            log.debug("snapshot %s with vorigin uses %dMB" % (vname, vsize))
            used += self.align(vsize, roundup=True)

        return used

    @property
    def size(self):
        """ The size of this VG """
        # TODO: just ask lvm if isModified returns False

        # sum up the sizes of the PVs and align to pesize
        size = 0
        for pv in self.pvs:
            log.debug("PV size == %s" % pv.size)
            size += max(0, self.align(pv.size - pv.format.peStart))

        return size

    @property
    def extents(self):
        """ Number of extents in this VG """
        # TODO: just ask lvm if isModified returns False

        return self.size / self.peSize

    @property
    def freeSpace(self):
        """ The amount of free space in this VG (in MB). """
        # TODO: just ask lvm if isModified returns False

        # total the sizes of any LVs
        log.debug("%s size is %dMB" % (self.name, self.size))
        used = sum(lv.size for lv in self.lvs) + self.snapshotSpace
        free = self.size - used
        log.debug("vg %s has %dMB free" % (self.name, free))
        return free

    @property
    def freeExtents(self):
        """ The number of free extents in this VG. """
        # TODO: just ask lvm if isModified returns False
        return self.freeSpace / self.peSize

    def align(self, size, roundup=None):
        """ Align a size to a multiple of physical extent size. """
        size = numeric_type(size)

        if roundup:
            round = math.ceil
        else:
            round = math.floor

        # we want Kbytes as a float for our math
        size *= 1024.0
        pesize = self.peSize * 1024.0
        return long((round(size / pesize) * pesize) / 1024)

    @property
    def pvs(self):
        """ A list of this VG's PVs """
        return self.parents[:]  # we don't want folks changing our list

    @property
    def lvs(self):
        """ A list of this VG's LVs """
        return self._lvs[:]     # we don't want folks changing our list

    @property
    def complete(self):
        """Check if the vg has all its pvs in the system
        Return True if complete.
        """
        # vgs with duplicate names are overcomplete, which is not what we want
        if self.hasDuplicate:
            return False

        return len(self.pvs) == self.pvCount or not self.exists


class LVMLogicalVolumeDevice(DMDevice):
    """ An LVM Logical Volume """
    _type = "lvmlv"
    _resizable = True
    _packages = ["lvm2"]

    def __init__(self, name, vgdev, size=None, uuid=None,
                 stripes=1, logSize=0, snapshotSpace=0,
                 format=None, exists=None, sysfsPath='',
                 grow=None, maxsize=None, percent=None,
                 singlePV=False):
        """ Create a LVMLogicalVolumeDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)
                vgdev -- volume group (LVMVolumeGroupDevice instance)

            Keyword Arguments:

                size -- the device's size (in MB)
                uuid -- the device's UUID
                stripes -- number of copies in the vg (>1 for mirrored lvs)
                logSize -- size of log volume (for mirrored lvs)
                snapshotSpace -- sum of sizes of snapshots of this lv
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                exists -- indicates whether this is an existing device
                singlePV -- if true, maps this lv to a single pv

                For new (non-existent) LVs only:

                    grow -- whether to grow this LV
                    maxsize -- maximum size for growable LV (in MB)
                    percent -- percent of VG space to take

        """
        if isinstance(vgdev, list):
            if len(vgdev) != 1:
                raise ValueError("constructor requires a single LVMVolumeGroupDevice instance")
            elif not isinstance(vgdev[0], LVMVolumeGroupDevice):
                raise ValueError("constructor requires a LVMVolumeGroupDevice instance")
        elif not isinstance(vgdev, LVMVolumeGroupDevice):
            raise ValueError("constructor requires a LVMVolumeGroupDevice instance")
        DMDevice.__init__(self, name, size=size, format=format,
                          sysfsPath=sysfsPath, parents=vgdev,
                          exists=exists)

        self.uuid = uuid
        self.snapshotSpace = snapshotSpace
        self.stripes = stripes
        self.logSize = logSize
        self.singlePV = singlePV

        self.req_grow = None
        self.req_max_size = 0
        self.req_size = 0   
        self.req_percent = 0

        if not self.exists:
            self.req_grow = grow
            self.req_max_size = numeric_type(maxsize)
            # XXX should we enforce that req_size be pe-aligned?
            self.req_size = self._size
            self.req_percent = numeric_type(percent)

        # here we go with the circular references
        self.vg._addLogVol(self)

    def __str__(self):
        s = DMDevice.__str__(self)
        s += ("  VG device = %(vgdev)r  percent = %(percent)s\n"
              "  mirrored = %(mirrored)s stripes = %(stripes)d"
              "  snapshot total =  %(snapshots)dMB\n"
              "  VG space used = %(vgspace)dMB" %
              {"vgdev": self.vg, "percent": self.req_percent,
               "mirrored": self.mirrored, "stripes": self.stripes,
               "snapshots": self.snapshotSpace, "vgspace": self.vgSpaceUsed })
        return s

    @property
    def dict(self):
        d = super(LVMLogicalVolumeDevice, self).dict
        if self.exists:
            d.update({"mirrored": self.mirrored, "stripes": self.stripes,
                      "snapshots": self.snapshotSpace,
                      "vgspace": self.vgSpaceUsed})
        else:
            d.update({"percent": self.req_percent})

        return d

    def writeKS(self, f, preexisting=False, noformat=False, s=None):
        args = ["--name=%s" % self.lvname,
                "--vgname=%s" % self.vg.name]

        if self.req_grow:
            args.extend(["--grow", "--size=%s" % (self.req_size or 1)])

            if self.req_max_size > 0:
                args.append("--maxsize=%s" % self.req_max_size)
        else:
            if self.req_percent > 0:
                args.append("--percent=%s" % self.req_percent)
            elif self.req_size > 0:
                args.append("--size=%s" % self.req_size)

        if preexisting:
            args.append("--useexisting")
        if noformat:
            args.append("--noformat")

        f.write("#logvol ")
        self.format.writeKS(f)
        f.write(" %s" % " ".join(args))
        if s:
            f.write(" %s" % s)

    @property
    def mirrored(self):
        return self.stripes > 1

    def _setSize(self, size):
        size = self.vg.align(numeric_type(size))
        log.debug("trying to set lv %s size to %dMB" % (self.name, size))
        if size <= (self.vg.freeSpace + self._size):
            self._size = size
            self.targetSize = size
        else:
            log.debug("failed to set size: %dMB short" % (size - (self.vg.freeSpace + self._size),))
            raise ValueError("not enough free space in volume group")

    size = property(StorageDevice._getSize, _setSize)

    @property
    def vgSpaceUsed(self):
        return self.size * self.stripes + self.logSize + self.snapshotSpace

    @property
    def vg(self):
        """ This Logical Volume's Volume Group. """
        return self.parents[0]

    @property
    def mapName(self):
        """ This device's device-mapper map name """
        # Thank you lvm for this lovely hack.
        return "%s-%s" % (self.vg.mapName, self._name.replace("-","--"))

    @property
    def path(self):
        """ Device node representing this device. """
        return "%s/%s" % (self._devDir, self.mapName)

    def getDMNode(self):
        """ Return the dm-X (eg: dm-0) device node for this device. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        return dm.dm_node_from_name(self.mapName)

    @property
    def name(self):
        """ This device's name. """
        return "%s-%s" % (self.vg.name, self._name)

    @property
    def lvname(self):
        """ The LV's name (not including VG name). """
        return self._name

    @property
    def complete(self):
        """ Test if vg exits and if it has all pvs. """
        return self.vg.complete

    def setupParents(self, orig=False):
        # parent is a vg, which has no formatting (or device for that matter)
        Device.setupParents(self, orig=orig)

    def _setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        lvm.lvactivate(self.vg.name, self._name)

    def _teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        lvm.lvdeactivate(self.vg.name, self._name)

    def _postTeardown(self, recursive=False):
        try:
            # It's likely that teardown of a VG will fail due to other
            # LVs being active (filesystems mounted, &c), so don't let
            # it bring everything down.
            StorageDevice._postTeardown(self, recursive=recursive)
        except Exception as e:
            if recursive:
                log.debug("vg %s teardown failed; continuing" % self.vg.name)
            else:
                raise

    def _statusWindow(self, intf=None, title="", msg=""):
        return self._progressWindow(intf=intf, title=title, msg=msg)

    def _create(self, w):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        # should we use --zero for safety's sake?
        if self.singlePV:
            lvm.lvcreate(self.vg.name, self._name, self.size, progress=w,
                         pvs=self._getSinglePV())
        else:
            lvm.lvcreate(self.vg.name, self._name, self.size, progress=w)

    def _preDestroy(self):
        StorageDevice._preDestroy(self)
        # set up the vg's pvs so lvm can remove the lv
        self.vg.setupParents(orig=True)

    def _destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        lvm.lvremove(self.vg.name, self._name)

    def _getSinglePV(self):
        # NOTE: This runs the pvs(8) command and outputs the PV device
        # name, VG name, and amount of free space on the PV in megabytes.
        # A change to the Size class will require more handling of the
        # units switch in this arg list.
        args = ["pvs", "--noheadings", "--nosuffix", "--aligned",
                "--units", "M", "-o", "pv_name,vg_name,pv_free"]
        buf = iutil.execWithCapture("lvm", args, stderr="/dev/tty5")

        for line in buf.splitlines():
            fields = filter(lambda x: x != '', line.strip().split())

            if len(fields) != 3 or fields[1] != self.vg.name:
                continue
            if float(fields[2]) >= self.size:
                return [fields[0]]             # lvm.lvcreate needs a list

        if hasattr(self.format, "mountpoint"):
            mountpoint = self.format.mountpoint
        else:
            mountpoint = self.format.name

        raise SinglePhysicalVolumeError("Single physical volume restriction "
                                        "for %(mountpoint)s on this platform. "
                                        "No physical volumes available in "
                                        "volume group %(vgname)s with "
                                        "%(size)d MB of available space."
                                        % {'mountpoint': mountpoint,
                                           'vgname': self.vg.name,
                                           'size': self.size})

    def resize(self, intf=None):
        log_method_call(self, self.name, status=self.status)
        self._preDestroy()

        # Setup VG parents (in case they are dmraid partitions for example)
        self.vg.setupParents(orig=True)

        if self.originalFormat.exists:
            self.originalFormat.teardown()
        if self.format.exists:
            self.format.teardown()

        udev_settle()
        lvm.lvresize(self.vg.name, self._name, self.size)

    def dracutSetupString(self):
        # Note no mapName usage here, this is a lvm cmdline name, which
        # is different (ofcourse)
        return "rd_LVM_LV=%s/%s" % (self.vg.name, self._name)


class MDRaidArrayDevice(StorageDevice):
    """ An mdraid (Linux RAID) device. """
    _type = "mdarray"
    _packages = ["mdadm"]

    def __init__(self, name, level=None, major=None, minor=None, size=None,
                 memberDevices=None, totalDevices=None,
                 uuid=None, format=None, exists=None,
                 parents=None, sysfsPath=''):
        """ Create a MDRaidArrayDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)

            Keyword Arguments:

                level -- the device's RAID level (a string, eg: '1' or 'raid1')
                parents -- list of member devices (StorageDevice instances)
                size -- the device's size (units/format TBD)
                uuid -- the device's UUID
                minor -- the device minor
                sysfsPath -- sysfs device path
                format -- a DeviceFormat instance
                exists -- indicates whether this is an existing device
        """
        StorageDevice.__init__(self, name, format=format, exists=exists,
                               major=major, minor=minor, size=size,
                               parents=parents, sysfsPath=sysfsPath)

        self.level = level
        if level == "container":
            self._type = "mdcontainer"
        elif level is not None:
            self.level = mdraid.raidLevel(level)

        # For new arrays check if we have enough members
        if (not exists and parents and
                len(parents) < mdraid.get_raid_min_members(self.level)):
            raise ValueError, P_("A RAID%d set requires at least %d member",
                                 "A RAID%d set requires at least %d members",
                                 mdraid.get_raid_min_members(self.level)) % \
                                 (self.level, mdraid.get_raid_min_members(self.level))

        self.uuid = uuid
        self._totalDevices = numeric_type(totalDevices)
        self._memberDevices = numeric_type(memberDevices)
        self.sysfsPath = "/devices/virtual/block/%s" % name
        self.chunkSize = 512.0 / 1024.0         # chunk size in MB
        self.superBlockSize = 2.0               # superblock size in MB

        self.createMetadataVer = "1.1"
        # bitmaps are not meaningful on raid0 according to mdadm-3.0.3
        self.createBitmap = self.level != 0

        # For container members probe size now, as we cannot determine it
        # when teared down.
        if self.parents and self.parents[0].type == "mdcontainer":
            self._size = self.currentSize
            self._type = "mdbiosraidarray"

        self.formatClass = get_device_format_class("mdmember")
        if not self.formatClass:
            raise DeviceError("cannot find class for 'mdmember'", self.name)

        if self.exists and self.uuid:
            # this is a hack to work around mdadm's insistence on giving
            # really high minors to arrays it has no config entry for
            open("/etc/mdadm.conf", "a").write("ARRAY %s UUID=%s\n"
                                                % (self.path, self.uuid))

    @property
    def smallestMember(self):
        try:
            smallest = sorted(self.devices, key=lambda d: d.size)[0]
        except IndexError:
            smallest = None
        return smallest

    @property
    def size(self):
        if not self.devices:
            return 0

        # For container members return probed size, as we cannot determine it
        # when teared down.
        if self.type == "mdbiosraidarray":
            return self._size

        size = 0
        smallestMemberSize = self.smallestMember.size - self.superBlockSize
        if not self.exists or not self.partedDevice:
            if self.level == mdraid.RAID0:
                size = self.memberDevices * smallestMemberSize
                size -= size % self.chunkSize
            elif self.level == mdraid.RAID1:
                size = smallestMemberSize
            elif self.level == mdraid.RAID4:
                size = (self.memberDevices - 1) * smallestMemberSize
                size -= size % self.chunkSize
            elif self.level == mdraid.RAID5:
                size = (self.memberDevices - 1) * smallestMemberSize
                size -= size % self.chunkSize
            elif self.level == mdraid.RAID6:
                size = (self.memberDevices - 2) * smallestMemberSize
                size -= size % self.chunkSize
            elif self.level == mdraid.RAID10:
                size = (self.memberDevices / 2.0) * smallestMemberSize
                size -= size % self.chunkSize
            log.debug("non-existant RAID %s size == %s" % (self.level, size))
        else:
            size = self.partedDevice.getSize()
            log.debug("existing RAID %s size == %s" % (self.level, size))

        return size

    @property
    def description(self):
        if self.level == mdraid.RAID0:
            levelstr = "stripe"
        elif self.level == mdraid.RAID1:
            levelstr = "mirror"
        else:
            levelstr = "raid%s" % self.level

        if self.type == "mdcontainer":
            return "BIOS RAID container"
        elif self.type == "mdbiosraidarray":
            return "BIOS RAID set (%s)" % levelstr
        else:
            return "MDRAID set (%s)" % levelstr

    def __str__(self):
        s = StorageDevice.__str__(self)
        s += ("  level = %(level)s  spares = %(spares)s\n"
              "  members = %(memberDevices)s\n"
              "  total devices = %(totalDevices)s" %
              {"level": self.level, "spares": self.spares,
               "memberDevices": self.memberDevices, "totalDevices": self.totalDevices})
        return s

    @property
    def dict(self):
        d = super(MDRaidArrayDevice, self).dict
        d.update({"level": self.level,
                  "spares": self.spares, "memberDevices": self.memberDevices,
                  "totalDevices": self.totalDevices})
        return d

    def writeKS(self, f, preexisting=False, noformat=False, s=None):
        args = ["--level=%s" % self.level,
                "--device=%s" % self.name]
        mems = []

        if self.spares > 0:
            args.append("--spares=%s" % self.spares)
        if preexisting:
            args.append("--useexisting")
        if noformat:
            args.append("--noformat")

        for mem in self.parents:
            mems.append("raid.%s" % mem.format.majorminor)

        f.write("#raid ")
        self.format.writeKS(f)
        f.write(" %s" % " ".join(args))
        f.write(" %s" % " ".join(mems))
        if s:
            f.write(" %s" % s)

    @property
    def mdadmConfEntry(self):
        """ This array's mdadm.conf entry. """
        if self.level is None or self.memberDevices is None or not self.uuid:
            raise DeviceError("array is not fully defined", self.name)

        # containers and the sets within must only have a UUID= parameter
        if self.type == "mdcontainer" or self.type == "mdbiosraidarray":
            fmt = "ARRAY %s UUID=%s\n"
            return fmt % (self.path, self.uuid)

        fmt = "ARRAY %s level=raid%d num-devices=%d UUID=%s\n"
        return fmt % (self.path, self.level, self.memberDevices, self.uuid)

    @property
    def totalDevices(self):
        """ Total number of devices in the array, including spares. """
        count = len(self.parents)
        if not self.exists:
            count = self._totalDevices
        return count

    def _getMemberDevices(self):
        return self._memberDevices

    def _setMemberDevices(self, number):
        if not isinstance(number, int):
            raise ValueError("memberDevices is an integer")

        if number > self.totalDevices:
            raise ValueError("memberDevices cannot be greater than totalDevices")
        self._memberDevices = number

    memberDevices = property(_getMemberDevices, _setMemberDevices,
                             doc="number of member devices")

    def _getSpares(self):
        spares = 0
        if self.memberDevices is not None:
            if self.totalDevices is not None:
                spares = self.totalDevices - self.memberDevices
            else:
                spares = self.memberDevices
                self._totalDevices = self.memberDevices
        return spares

    def _setSpares(self, spares):
        # FIXME: this is too simple to be right
        if self.totalDevices > spares:
            self.memberDevices = self.totalDevices - spares

    spares = property(_getSpares, _setSpares)

    def updateSysfsPath(self):
        """ Update this device's sysfs path. """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        if self.status:
            self.sysfsPath = "/devices/virtual/block/%s" % self.name
        else:
            self.sysfsPath = ''

    def _addDevice(self, device):
        """ Add a new member device to the array.

            XXX This is for use when probing devices, not for modification
                of arrays.
        """
        log_method_call(self,
                        self.name,
                        device=device.name,
                        status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        if not isinstance(device.format, self.formatClass):
            raise ValueError("invalid device format for mdraid member")

        if self.uuid and device.format.mdUuid != self.uuid:
            raise ValueError("cannot add member with non-matching UUID")

        if device in self.devices:
            raise ValueError("device is already a member of this array")

        # we added it, so now set up the relations
        self.devices.append(device)
        device.addChild()

        device.setup()
        udev_settle()

        if self.spares > 0:
            # mdadm doesn't like it when you try to incrementally add spares
            return

        try:
            mdraid.mdadd(device.path)
            # mdadd causes udev events
            udev_settle()
        except MDRaidError as e:
            log.warning("failed to add member %s to md array %s: %s"
                        % (device.path, self.path, e))

        if self.status:
            # we always probe since the device may not be set up when we want
            # information about it
            self._size = self.currentSize

    def _removeDevice(self, device):
        """ Remove a component device from the array.

            XXX This is for use by clearpart, not for reconfiguration.
        """
        log_method_call(self,
                        self.name,
                        device=device.name,
                        status=self.status)

        if device not in self.devices:
            raise ValueError("cannot remove non-member device from array")

        self.devices.remove(device)
        device.removeChild()

    @property
    def status(self):
        """ This device's status.

            For now, this should return a boolean:
                True    the device is open and ready for use
                False   the device is not open
        """
        # check the status in sysfs
        status = False
        if not self.exists:
            return status

        state_file = "/sys/%s/md/array_state" % self.sysfsPath
        if os.access(state_file, os.R_OK):
            state = open(state_file).read().strip()
            log.debug("%s state is %s" % (self.name, state))
            if state in ("clean", "active", "active-idle", "readonly", "read-auto"):
                status = True
            # mdcontainers have state inactive when started (clear if stopped)
            if self.type == "mdcontainer" and state == "inactive":
                status = True

        return status

    @property
    def degraded(self):
        """ Return True if the array is running in degraded mode. """
        rc = False
        degraded_file = "/sys/%s/md/degraded" % self.sysfsPath
        if os.access(degraded_file, os.R_OK):
            val = open(degraded_file).read().strip()
            log.debug("%s degraded is %s" % (self.name, val))
            if val == "1":
                rc = True

        return rc

    @property
    def devices(self):
        """ Return a list of this array's member device instances. """
        return self.parents

    def _setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        disks = []
        for member in self.devices:
            member.setup(orig=orig)
            disks.append(member.path)

        mdraid.mdactivate(self.path,
                          members=disks,
                          super_minor=self.minor,
                          uuid=self.uuid)

    def teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        # we don't really care about the return value of _preTeardown here.
        # see comment just above mddeactivate call
        self._preTeardown(recursive=recursive)

        # Since BIOS RAID sets (containers in mdraid terminology) never change
        # there is no need to stop them and later restart them. Not stopping
        # (and thus also not starting) them also works around bug 523334
        if self.type == "mdcontainer" or self.type == "mdbiosraidarray":
            return

        # We don't really care what the array's state is. If the device
        # file exists, we want to deactivate it. mdraid has too many
        # states.
        if self.exists and os.path.exists(self.path):
            mdraid.mddeactivate(self.path)

        self._postTeardown(recursive=recursive)

    def preCommitFixup(self, *args, **kwargs):
        """ Determine create parameters for this set """
        mountpoints = kwargs.pop("mountpoints")
        log_method_call(self, self.name, mountpoints)

        if "/boot" in mountpoints:
            bootmountpoint = "/boot"
        else:
            bootmountpoint = "/"

        # If we are used to boot from we cannot use 1.1 metadata
        if getattr(self.format, "mountpoint", None) == bootmountpoint or \
           getattr(self.format, "mountpoint", None) == "/boot/efi" or \
           self.format.type == "prepboot":
            self.createMetadataVer = "1.0"

        # Bitmaps are not useful for swap and small partitions
        if self.size < 1000 or self.format.type == "swap":
            self.createBitmap = False

    def _statusWindow(self, intf=None, title="", msg=""):
        return self._progressWindow(intf=intf, title=title, msg=msg)

    def _postCreate(self):
        StorageDevice._postCreate(self)
        info = udev_get_block_device(self.sysfsPath)
        self.uuid = udev_device_get_md_uuid(info)
        for member in self.devices:
            member.mdUuid = self.uuid

    def _create(self, w):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        disks = [disk.path for disk in self.devices]
        spares = len(self.devices) - self.memberDevices
        mdraid.mdcreate(self.path,
                        self.level,
                        disks,
                        spares,
                        metadataVer=self.createMetadataVer,
                        bitmap=self.createBitmap,
                        progress=w)

    @property
    def formatArgs(self):
        formatArgs = []
        if self.format.type == "ext2":
            if self.level == mdraid.RAID5:
                formatArgs = ['-R',
                              'stride=%d' % ((self.memberDevices - 1) * 16)]
            if self.level == mdraid.RAID4:
                formatArgs = ['-R',
                              'stride=%d' % ((self.memberDevices - 1) * 16)]
            elif self.level == mdraid.RAID0:
                formatArgs = ['-R',
                              'stride=%d' % (self.memberDevices * 16)]

    @property
    def mediaPresent(self):
        # Containers should not get any format handling done
        # (the device node does not allow read / write calls)
        if self.type == "mdcontainer":
            return False
        # BIOS RAID sets should show as present even when teared down
        elif self.type == "mdbiosraidarray":
            return True
        else:
            return self.partedDevice is not None

    @property
    def model(self):
        return self.description

    @property
    def partitionable(self):
        return self.type == "mdbiosraidarray"

    @property
    def isDisk(self):
        return self.type == "mdbiosraidarray"

    def dracutSetupString(self):
        return "rd_MD_UUID=%s" % self.uuid


class DMRaidArrayDevice(DMDevice):
    """ A dmraid (device-mapper RAID) device """
    _type = "dm-raid array"
    _packages = ["dmraid"]
    _partitionable = True
    _isDisk = True

    def __init__(self, name, raidSet=None, format=None,
                 size=None, parents=None, sysfsPath=''):
        """ Create a DMRaidArrayDevice instance.

            Arguments:

                name -- the dmraid name also the device node's basename

            Keyword Arguments:

                raidSet -- the RaidSet object from block
                parents -- a list of the member devices
                sysfsPath -- sysfs device path
                size -- the device's size
                format -- a DeviceFormat instance
        """
        if isinstance(parents, list):
            for parent in parents:
                if not parent.format or parent.format.type != "dmraidmember":
                    raise ValueError("parent devices must contain dmraidmember format")
        DMDevice.__init__(self, name, format=format, size=size,
                          parents=parents, sysfsPath=sysfsPath, exists=True)

        self.formatClass = get_device_format_class("dmraidmember")
        if not self.formatClass:
            raise StorageError("cannot find class for 'dmraidmember'")

        self._raidSet = raidSet

    @property
    def raidSet(self):
        return self._raidSet

    def _addDevice(self, device):
        """ Add a new member device to the array.

            XXX This is for use when probing devices, not for modification
                of arrays.
        """
        log_method_call(self, self.name, device=device.name, status=self.status)

        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        if not isinstance(device.format, self.formatClass):
            raise ValueError("invalid device format for dmraid member")

        if device in self.members:
            raise ValueError("device is already a member of this array")

        # we added it, so now set up the relations
        self.devices.append(device)
        device.addChild()

    @property
    def members(self):
        return self.parents

    @property
    def devices(self):
        """ Return a list of this array's member device instances. """
        return self.parents

    def deactivate(self):
        """ Deactivate the raid set. """
        log_method_call(self, self.name, status=self.status)
        # This call already checks if the set is not active.
        self._raidSet.deactivate()

    def activate(self):
        """ Activate the raid set. """
        log_method_call(self, self.name, status=self.status)
        # This call already checks if the set is active.
        self._raidSet.activate(mknod=True)
        udev_settle()

    def _setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        self.activate()

    def teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        if not self._preTeardown(recursive=recursive):
            return

        log.debug("not tearing down dmraid device %s" % self.name)

    @property
    def description(self):
        return "BIOS RAID set (%s)" % self._raidSet.rs.set_type

    @property
    def model(self):
        return self.description

    def dracutSetupString(self):
        return "rd_DM_UUID=%s" % self.name

class MultipathDevice(DMDevice):
    """ A multipath device """
    _type = "dm-multipath"
    _packages = ["device-mapper-multipath"]
    _services = ["multipathd"]
    _partitionable = True
    _isDisk = True

    def __init__(self, name, info, format=None, size=None,
                 parents=None, sysfsPath=''):
        """ Create a MultipathDevice instance.

            Arguments:

                name -- the device name (generally a device node's basename)
                info -- the udev info for this device

            Keyword Arguments:

                sysfsPath -- sysfs device path
                size -- the device's size
                format -- a DeviceFormat instance
                parents -- a list of the backing devices (Device instances)
        """

        self._info = info
        self.setupIdentity()
        DMDevice.__init__(self, name, format=format, size=size,
                          parents=parents, sysfsPath=sysfsPath,
                          exists=True)

        self.config = {
            'wwid' : self.identity,
            'alias' : self.name,
            'mode' : '0600',
            'uid' : '0',
            'gid' : '0',
        }

    def setupIdentity(self):
        """ Adds identifying remarks to MultipathDevice object.
        
            May be overridden by a sub-class for e.g. RDAC handling.
        """
        self._identity = self._info.get("ID_SERIAL_RAW", self._info.get("ID_SERIAL_SHORT"))

    @property
    def identity(self):
        """ Get identity set with setupIdentityFromInfo()
        
            May be overridden by a sub-class for e.g. RDAC handling.
        """
        if not hasattr(self, "_identity"):
            raise RuntimeError, "setupIdentityFromInfo() has not been called."
        return self._identity

    @property
    def wwid(self):
        identity = self.identity
        ret = []
        while identity:
            ret.append(identity[:2])
            identity = identity[2:]
        return ":".join(ret)

    @property
    def model(self):
        if not self.parents:
            return ""
        return self.parents[0].model

    @property
    def vendor(self):
        if not self.parents:
            return ""
        return self.parents[0].vendor

    @property
    def description(self):
        return "WWID %s" % (self.wwid,)

    def addParent(self, parent):
        """ Add a parent device to the mpath. """
        log_method_call(self, self.name, status=self.status)
        if self.status:
            self.teardown()
            self.parents.append(parent)
            self.setup()
        else:
            self.parents.append(parent)

    def deactivate(self):
        """ 
        This is never called, included just for documentation.

        If we called this during teardown(), we wouldn't be able to get parted
        object because /dev/mapper/mpathX wouldn't exist.
        """
        if self.exists and os.path.exists(self.path):
            #self.teardownPartitions()
            #rc = iutil.execWithRedirect("multipath",
            #                    ['-f', self.name],
            #                    stdout = "/dev/tty5",
            #                    stderr = "/dev/tty5")
            #if rc:
            #    raise MPathError("multipath deactivation failed for '%s'" %
            #                    self.name)
            bdev = block.getDevice(self.name)
            devmap = block.getMap(major=bdev[0], minor=bdev[1])
            if devmap.open_count:
                return
            try:
                block.removeDeviceMap(devmap)
            except Exception as e:
                raise MPathError("failed to tear down multipath device %s: %s"
                                % (self.name, e))

    def _setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        udev_settle()
        rc = iutil.execWithRedirect("multipath",
                            [self.name],
                            stdout = "/dev/tty5",
                            stderr = "/dev/tty5")
        if rc:
            raise MPathError("multipath activation failed for '%s'" %
                            self.name)

    def _postSetup(self):
        StorageDevice._postSetup(self)
        self.setupPartitions()
        udev_settle()

class NoDevice(StorageDevice):
    """ A nodev device for nodev filesystems like tmpfs. """
    _type = "nodev"

    def __init__(self, format=None):
        """ Create a NoDevice instance.

            Arguments:

            Keyword Arguments:

                format -- a DeviceFormat instance
        """
        if format:
            name = format.type
        else:
            name = "none"

        StorageDevice.__init__(self, name, format=format, exists=True)

    @property
    def path(self):
        """ Device node representing this device. """
        return self.name

    def setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)

    def teardown(self, recursive=False):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        # just make sure the format is unmounted
        self._preTeardown(recursive=recursive)

    def create(self, intf=None):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)

    def destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        self._preDestroy()


class FileDevice(StorageDevice):
    """ A file on a filesystem.

        This exists because of swap files.
    """
    _type = "file"
    _devDir = ""

    def __init__(self, path, format=None, size=None,
                 exists=None, parents=None):
        """ Create a FileDevice instance.

            Arguments:

                path -- full path to the file

            Keyword Arguments:

                format -- a DeviceFormat instance
                size -- the file size (units TBD)
                parents -- a list of required devices (Device instances)
                exists -- indicates whether this is an existing device
        """
        if not path.startswith("/"):
            raise ValueError("FileDevice requires an absolute path")

        StorageDevice.__init__(self, path, format=format, size=size,
                               exists=exists, parents=parents)

    @property
    def fstabSpec(self):
        return self.name

    @property
    def path(self):
        root = ""
        try:
            status = self.parents[0].format.status
        except (AttributeError, IndexError):
            # either this device has no parents or something is wrong with
            # the first one
            status = (os.access(self.name, os.R_OK) and
                      self.parents in ([], None))
        else:
            # this is the actual active mountpoint
            root = self.parents[0].format._mountpoint
            # trim the mountpoint down to the chroot since we already have
            # the otherwise fully-qualified path
            mountpoint = self.parents[0].format.mountpoint
            while mountpoint.endswith("/"):
                mountpoint = mountpoint[:-1]
            if mountpoint:
                root = root[:-len(mountpoint)]

        return os.path.normpath("%s%s" % (root, self.name))

    def _preSetup(self, orig=False):
        if self.format and self.format.exists and not self.format.status:
            self.format.device = self.path

        return StorageDevice._preSetup(self, orig=orig)

    def _preTeardown(self, recursive=None):
        if self.format and self.format.exists and not self.format.status:
            self.format.device = self.path

        return StorageDevice._preTeardown(self, recursive=recursive)

    def _create(self, w):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        fd = os.open(self.path, os.O_RDWR)
        buf = '\0' * 1024 * 1024 * self.size
        os.write(fd, buf)

    def _destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
        os.unlink(self.path)


class DirectoryDevice(FileDevice):
    """ A directory on a filesystem.

        This exists because of bind mounts.
    """
    _type = "directory"

    def _create(self, w):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        iutil.mkdirChain(self.path)


class LoopDevice(StorageDevice):
    """ A loop device. """
    _type = "loop"

    def __init__(self, name=None, format=None, size=None, sysfsPath=None,
                 exists=None, parents=None):
        """ Create a LoopDevice instance.

            Arguments:

                name -- the device's name

            Keyword Arguments:

                format -- a DeviceFormat instance
                size -- the device's size in MB
                parents -- a list of required devices (Device instances)
                exists -- indicates whether this is an existing device


            Loop devices always exist.
        """
        if not parents:
            raise ValueError("LoopDevice requires a backing device")

        if not name:
            # set up a temporary name until we've activated the loop device
            name = "tmploop%d" % Device._id

        StorageDevice.__init__(self, name, format=format, size=size,
                               exists=True, parents=parents)

    def updateName(self):
        """ Update this device's name. """
        if not self.slave.status:
            # if the backing device is inactive, so are we
            return self.name

        if self.name.startswith("loop"):
            # if our name is loopN we must already be active
            return self.name

        name = loop.get_loop_name(self.slave.path)
        if name.startswith("loop"):
            self._name = name

        return self.name

    @property
    def status(self):
        return (self.slave.status and
                self.name.startswith("loop") and
                loop.get_loop_name(self.slave.path) == self.name)

    @property
    def size(self):
        return self.slave.size

    def _setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)
        loop.loop_setup(self.slave.path)

    def _postSetup(self):
        StorageDevice._postSetup(self)
        self.updateName()
        self.updateSysfsPath()

    def _teardown(self, recursive=False):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)
        loop.loop_teardown(self.path)

    def _postTeardown(self, recursive=False):
        StorageDevice._postTeardown(self, recursive=recursive)
        self._name = "tmploop%d" % self.id
        self.sysfsPath = ''

    @property
    def slave(self):
        return self.parents[0]


class iScsiDiskDevice(DiskDevice, NetworkStorageDevice):
    """ An iSCSI disk. """
    _type = "iscsi"
    _packages = ["iscsi-initiator-utils", "dracut-network"]

    def __init__(self, device, **kwargs):
        self.node = kwargs.pop("node")
        self.ibft = kwargs.pop("ibft")
        self.initiator = kwargs.pop("initiator")
        DiskDevice.__init__(self, device, **kwargs)
        NetworkStorageDevice.__init__(self, host_address=self.node.address)
        log.debug("created new iscsi disk %s %s:%d" % (self.node.name, self.node.address, self.node.port))

    def dracutSetupString(self):
        if self.ibft:
            return "iscsi_firmware"

        address = self.node.address
        # surround ipv6 addresses with []
        if ":" in address:
            address = "[%s]" % address

        netroot="netroot=iscsi:"
        auth = self.node.getAuth()
        if auth:
            netroot += "%s:%s" % (auth.username, auth.password)
            if len(auth.reverse_username) or len(auth.reverse_password):
                netroot += ":%s:%s" % (auth.reverse_username,
                                       auth.reverse_password)

        netroot += "@%s::%d::%s" % (address, self.node.port, self.node.name)

        netroot += " iscsi_initiator=%s" % self.initiator

        return netroot

class FcoeDiskDevice(DiskDevice, NetworkStorageDevice):
    """ An FCoE disk. """
    _type = "fcoe"
    _packages = ["fcoe-utils", "dracut-network"]

    def __init__(self, device, **kwargs):
        self.nic = kwargs.pop("nic")
        self.identifier = kwargs.pop("identifier")
        DiskDevice.__init__(self, device, **kwargs)
        NetworkStorageDevice.__init__(self, nic=self.nic)
        log.debug("created new fcoe disk %s @ %s" % (device, self.nic))

    def dracutSetupString(self):
        dcb = True

        from .fcoe import fcoe
        for nic, dcb in fcoe().nics:
            if nic == self.nic:
                break

        if dcb:
            dcbOpt = "dcb"
        else:
            dcbOpt = "nodcb"

        return "fcoe=%s:%s" % (self.nic, dcbOpt)


class OpticalDevice(StorageDevice):
    """ An optical drive, eg: cdrom, dvd+r, &c.

        XXX Is this useful?
    """
    _type = "cdrom"

    def __init__(self, name, major=None, minor=None, exists=None,
                 format=None, parents=None, sysfsPath='', vendor="",
                 model=""):
        StorageDevice.__init__(self, name, format=format,
                               major=major, minor=minor, exists=True,
                               parents=parents, sysfsPath=sysfsPath,
                               vendor=vendor, model=model)

    @property
    def mediaPresent(self):
        """ Return a boolean indicating whether or not the device contains
            media.
        """
        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError as e:
            # errno 123 = No medium found
            if e.errno == 123:
                return False
            else:
                return True
        else:
            os.close(fd)
            return True

    def eject(self):
        """ Eject the drawer. """
        from pyanaconda import _isys

        log_method_call(self, self.name, status=self.status)
        if not self.exists:
            raise DeviceError("device has not been created", self.name)

        #try to umount and close device before ejecting
        self.teardown()

        if flags.cmdline.has_key('noeject'):
            log.info("noeject in effect, not ejecting cdrom")
            return

        # Make a best effort attempt to do the eject.  If it fails, it's not
        # critical.
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)

        try:
            _isys.ejectcdrom(fd)
        except SystemError as e:
            log.warning("error ejecting cdrom %s: %s" % (self.name, e))

        os.close(fd)


class ZFCPDiskDevice(DiskDevice):
    """ A mainframe ZFCP disk. """
    _type = "zfcp"

    def __init__(self, device, **kwargs):
        self.hba_id = kwargs.pop("hba_id")
        self.wwpn = kwargs.pop("wwpn")
        self.fcp_lun = kwargs.pop("fcp_lun")
        DiskDevice.__init__(self, device, **kwargs)

    def __str__(self):
        s = DiskDevice.__str__(self)
        s += ("  hba_id = %(hba_id)s  wwpn = %(wwpn)s  fcp_lun = %(fcp_lun)s" %
              {"hba_id": self.hba_id,
               "wwpn": self.wwpn,
               "fcp_lun": self.fcp_lun})
        return s

    @property
    def description(self):
        return "FCP device %(device)s with WWPN %(wwpn)s and LUN %(lun)s" \
               % {'device': self.hba_id,
                  'wwpn': self.wwpn,
                  'lun': self.fcp_lun}

    def dracutSetupString(self):
        return "rd_ZFCP=%s,%s,%s" % (self.hba_id, self.wwpn, self.fcp_lun,)


class DASDDevice(DiskDevice):
    """ A mainframe DASD. """
    _type = "dasd"

    def __init__(self, device, **kwargs):
        self.busid = kwargs.pop('busid')
        self.opts = kwargs.pop('opts')
        self.dasd = kwargs.pop('dasd')
        DiskDevice.__init__(self, device, **kwargs)

        if self.dasd:
            self.dasd.addDASD(self)

    @property
    def description(self):
        return "DASD device %s" % self.busid

    def getOpts(self):
        return map(lambda (k, v): "%s=%s" % (k, v,), self.opts.items())

    def dracutSetupString(self):
        conf = "/etc/dasd.conf"
        opts = {}

        if os.path.isfile(conf):
            f = open(conf)
            lines = filter(lambda y: not y.startswith('#') and y != '',
                           map(lambda x: x.strip(), f.readlines()))
            f.close()

            for line in lines:
                parts = line.split()
                if parts != []:
                    opts[parts[0]] = parts

        if self.busid in opts.keys():
            return "rd_DASD=%s" % ",".join(opts[self.busid])
        else:
            return "rd_DASD=%s" % ",".join([self.busid] + self.getOpts())

class NFSDevice(StorageDevice, NetworkStorageDevice):
    """ An NFS device """
    _type = "nfs"
    _packages = ["dracut-network"]

    def __init__(self, device, format=None, parents=None):
        # we could make host/ip, path, &c but will anything use it?
        StorageDevice.__init__(self, device, format=format, parents=parents)
        NetworkStorageDevice.__init__(self, device.split(":")[0])

    @property
    def path(self):
        """ Device node representing this device. """
        return self.name

    def setup(self, intf=None, orig=False):
        """ Open, or set up, a device. """
        log_method_call(self, self.name, orig=orig, status=self.status,
                        controllable=self.controllable)

    def teardown(self, recursive=None):
        """ Close, or tear down, a device. """
        log_method_call(self, self.name, status=self.status,
                        controllable=self.controllable)

    def create(self, intf=None):
        """ Create the device. """
        log_method_call(self, self.name, status=self.status)
        self._preCreate()

    def destroy(self):
        """ Destroy the device. """
        log_method_call(self, self.name, status=self.status)
