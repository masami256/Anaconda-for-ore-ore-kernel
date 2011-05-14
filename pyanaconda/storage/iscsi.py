#
# iscsi.py - iscsi class
#
# Copyright (C) 2005, 2006  IBM, Inc.  All rights reserved.
# Copyright (C) 2006  Red Hat, Inc.  All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from pyanaconda.constants import *
from udev import *
import os
from pyanaconda import iutil
from pyanaconda.flags import flags
import logging
import shutil
import time
import hashlib
import random
log = logging.getLogger("anaconda")

import gettext
_ = lambda x: gettext.ldgettext("anaconda", x)

has_libiscsi = True
try:
    import libiscsi
except ImportError:
    has_libiscsi = False

# Note that stage2 copies all files under /sbin to /usr/sbin
ISCSID=""
INITIATOR_FILE="/etc/iscsi/initiatorname.iscsi"

ISCSI_MODULES=['cxgb3i', 'bnx2i', 'be2iscsi']

def has_iscsi():
    global ISCSID

    if not os.access("/sys/module/iscsi_tcp", os.X_OK):
        return False

    if not ISCSID:
        location = iutil.find_program_in_path("iscsid")
        if not location:
            return False
        ISCSID = location
        log.info("ISCSID is %s" % (ISCSID,))

    return True

def randomIname():
    """Generate a random initiator name the same way as iscsi-iname"""

    s = "iqn.1994-05.com.domain:01."
    m = hashlib.md5()
    u = os.uname()
    for i in u:
        m.update(i)
    dig = m.hexdigest()

    for i in range(0, 6):
        s += dig[random.randrange(0, 32)]
    return s

class iscsi(object):
    """ iSCSI utility class.

        This class will automatically discover and login to iBFT (or
        other firmware) configured iscsi devices when the startup() method
        gets called. It can also be used to manually configure iscsi devices
        through the discover() and log_into_node() methods.

        As this class needs to make sure certain things like starting iscsid
        and logging in to firmware discovered disks only happens once
        and as it keeps a global list of all iSCSI devices it is implemented as
        a Singleton.
    """

    def __init__(self):
        # This list contains all nodes
        self.nodes = []
        # This list contains nodes discovered through iBFT (or other firmware)
        self.ibftNodes = []
        self._initiator = ""
        self.initiatorSet = False
        self.started = False

        if flags.ibft:
            try:
                initiatorname = libiscsi.get_firmware_initiator_name()
                self._initiator = initiatorname
                self.initiatorSet = True
            except:
                pass

    # So that users can write iscsi() to get the singleton instance
    def __call__(self):
        return self

    def _getInitiator(self):
        if self._initiator != "":
            return self._initiator

        return randomIname()

    def _setInitiator(self, val):
        if self.initiatorSet and val != self._initiator:
            raise ValueError, _("Unable to change iSCSI initiator name once set")
        if len(val) == 0:
            raise ValueError, _("Must provide an iSCSI initiator name")
        self._initiator = val

    initiator = property(_getInitiator, _setInitiator)

    def _startIBFT(self, intf = None):
        if not flags.ibft:
            return

        try:
            found_nodes = libiscsi.discover_firmware()
        except:
            # an exception here means there is no ibft firmware, just return
            return

        for node in found_nodes:
            try:
                node.login()
                log.info("iscsi._startIBFT logged in to %s %s %s" % (node.name, node.address, node.port))
                self.nodes.append(node)
                self.ibftNodes.append(node)
            except IOError, e:
                log.error("Could not log into ibft iscsi target %s: %s" %
                          (node.name, str(e)))
                pass

        self.stabilize(intf)

    def stabilize(self, intf = None):
        # Wait for udev to create the devices for the just added disks
        if intf:
            w = intf.waitWindow(_("Scanning iSCSI nodes"),
                                _("Scanning iSCSI nodes"))
        # It is possible when we get here the events for the new devices
        # are not send yet, so sleep to make sure the events are fired
        time.sleep(2)
        udev_settle()
        if intf:
            w.pop()

    def startup(self, intf = None):
        if self.started:
            return

        if not has_iscsi():
            return

        if self._initiator == "":
            log.info("no initiator set")
            return

        if intf:
            w = intf.waitWindow(_("Initializing iSCSI initiator"),
                                _("Initializing iSCSI initiator"))

        log.debug("Setting up %s" % (INITIATOR_FILE, ))
        log.info("iSCSI initiator name %s", self.initiator)
        if os.path.exists(INITIATOR_FILE):
            os.unlink(INITIATOR_FILE)
        if not os.path.isdir("/etc/iscsi"):
            os.makedirs("/etc/iscsi", 0755)
        fd = os.open(INITIATOR_FILE, os.O_RDWR | os.O_CREAT)
        os.write(fd, "InitiatorName=%s\n" %(self.initiator))
        os.close(fd)
        self.initiatorSet = True

        for dir in ['ifaces','isns','nodes','send_targets','slp','static']:
            fulldir = "/var/lib/iscsi/%s" % (dir,)
            if not os.path.isdir(fulldir):
                os.makedirs(fulldir, 0755)

        log.info("iSCSI startup")
        iutil.execWithRedirect('modprobe', ['-a'] + ISCSI_MODULES,
                               stdout="/dev/tty5", stderr="/dev/tty5")
        # brcm_iscsiuio is needed by Broadcom offload cards (bnx2i). Currently
        # not present in iscsi-initiator-utils for Fedora.
        try:
            brcm_iscsiuio = iutil.find_program_in_path('brcm_iscsiuio',
                                                   raise_on_error=True)
        except RuntimeError:
            log.info("iscsi: brcm_iscsiuio not found.")
        else:
            log.debug("iscsi: brcm_iscsiuio is at %s" % brcm_iscsiuio)
            iutil.execWithRedirect(brcm_iscsiuio, [],
                                   stdout="/dev/tty5", stderr="/dev/tty5")
        # run the daemon
        iutil.execWithRedirect(ISCSID, [],
                               stdout="/dev/tty5", stderr="/dev/tty5")
        time.sleep(1)

        if intf:
            w.pop()

        self._startIBFT(intf)
        self.started = True

    def discover(self, ipaddr, port="3260", username=None, password=None,
                  r_username=None, r_password=None, intf=None):
        """
        Discover iSCSI nodes on the target.

        Returns list of new found nodes.
        """
        authinfo = None
        found = 0
        logged_in = 0

        if not has_iscsi():
            raise IOError, _("iSCSI not available")
        if self._initiator == "":
            raise ValueError, _("No initiator name set")

        if username or password or r_username or r_password:
            # Note may raise a ValueError
            authinfo = libiscsi.chapAuthInfo(username=username,
                                             password=password,
                                             reverse_username=r_username,
                                             reverse_password=r_password)
        self.startup(intf)

        # Note may raise an IOError
        found_nodes = libiscsi.discover_sendtargets(address=ipaddr,
                                                    port=int(port),
                                                    authinfo=authinfo)
        # only return the nodes we are not logged into yet
        return [n for n in found_nodes if n not in self.nodes]

    def log_into_node(self, node, username=None, password=None,
                  r_username=None, r_password=None, intf=None):
        """
        Raises IOError.
        """
        rc = False # assume failure
        msg = ""

        if intf:
            w = intf.waitWindow(_("Logging in to iSCSI node"),
                                _("Logging in to iSCSI node %s") % node.name)
        try:
            authinfo = None
            if username or password or r_username or r_password:
                # may raise a ValueError
                authinfo = libiscsi.chapAuthInfo(username=username,
                                                 password=password,
                                                 reverse_username=r_username,
                                                 reverse_password=r_password)
            node.setAuth(authinfo)
            node.login()
            rc = True
            log.info("iSCSI: logged into %s %s:%s" % (node.name,
                                                      node.address,
                                                      node.port))
            self.nodes.append(node)
        except (IOError, ValueError) as e:
            msg = str(e)
            log.warning("iSCSI: could not log into %s: %s" % (node.name, msg))
        if intf:
            w.pop()

        return (rc, msg)

    def writeKS(self, f):
        if not self.initiatorSet:
            return
        f.write("iscsiname %s\n" %(self.initiator,))
        for n in self.nodes:
            f.write("iscsi --ipaddr %s --port %s --target %s" %
                    (n.address, n.port, n.name))
            auth = n.getAuth()
            if auth:
                f.write(" --user %s" % auth.username)
                f.write(" --password %s" % auth.password)
                if len(auth.reverse_username):
                    f.write(" --reverse-user %s" % auth.reverse_username)
                if len(auth.reverse_password):
                    f.write(" --reverse-password %s" % auth.reverse_password)
            f.write("\n")

    def write(self, instPath, anaconda):
        if not self.initiatorSet:
            return

        # set iscsi nodes to autostart
        root = anaconda.storage.rootDevice
        for node in self.nodes:
            autostart = True
            disks = self.getNodeDisks(node, anaconda.storage)
            for disk in disks:
                # nodes used for root get started by the initrd
                if root.dependsOn(disk):
                    autostart = False

            if autostart:
                node.setParameter("node.startup", "automatic")

        if not os.path.isdir(instPath + "/etc/iscsi"):
            os.makedirs(instPath + "/etc/iscsi", 0755)
        fd = os.open(instPath + INITIATOR_FILE, os.O_RDWR | os.O_CREAT)
        os.write(fd, "InitiatorName=%s\n" %(self.initiator))
        os.close(fd)

        # copy "db" files.  *sigh*
        if os.path.isdir(instPath + "/var/lib/iscsi"):
            shutil.rmtree(instPath + "/var/lib/iscsi")
        if os.path.isdir("/var/lib/iscsi"):
            shutil.copytree("/var/lib/iscsi", instPath + "/var/lib/iscsi",
                            symlinks=True)

    def getNode(self, name, address, port):
        for node in self.nodes:
            if node.name == name and node.address == address and \
               node.port == int(port):
                return node

        return None

    def getNodeDisks(self, node, storage):
        nodeDisks = []
        iscsiDisks = storage.devicetree.getDevicesByType("iscsi")
        for disk in iscsiDisks:
            if disk.node == node:
                nodeDisks.append(disk)

        return nodeDisks

# Create iscsi singleton
iscsi = iscsi()

# vim:tw=78:ts=4:et:sw=4
