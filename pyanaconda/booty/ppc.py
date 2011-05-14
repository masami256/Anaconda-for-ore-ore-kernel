import string
import os

from pyanaconda.booty import BootyNoKernelWarning
from bootloaderInfo import *
from pyanaconda import iutil

class ppcBootloaderInfo(bootloaderInfo):
    def getBootDevs(self, bl):
        import parted

        retval = []
        machine = iutil.getPPCMachine()

        if machine == 'pSeries':
            for dev in self.storage.fsset.devices:
                if dev.format.type == "prepboot":
                    retval.append(dev.path)
        elif machine == 'PMac':
            for dev in self.storage.fsset.devices:
                if dev.format.type == "hfs" and dev.format.bootable:
                    retval.append(dev.path)

        if len(retval) == 0:
            # Try to get a boot device; bplan OF understands ext3
            if machine == 'Pegasos' or machine == 'Efika':
                try:
                    device = self.storage.mountpoints["/boot"]
                except KeyError:
                    # Try / if we don't have this we're not going to work
                    device = self.storage.rootDevice

                retval.append(device.path)
            else:
                if bl.getDevice():
                    d = bl.getDevice()
                    retval.append(self.storage.devicetree.getDeviceByName(d).path)

        return retval

    def writeYaboot(self, instRoot, bl, kernelList, 
                    chainList, defaultDev):

        yabootTarget = string.join(self.getBootDevs(bl))

        try:
            bootDev = self.storage.mountpoints["/boot"]

            cf = "/boot/etc/yaboot.conf"
            cfPath = ""
            if not os.path.isdir(instRoot + "/boot/etc"):
                os.mkdir(instRoot + "/boot/etc")
        except KeyError:
            bootDev = self.storage.rootDevice

            cfPath = "/boot"
            cf = "/etc/yaboot.conf"

        if bootDev.type == "mdarray":
            partNumber = bootDev.parents[0].partedPartition.number
        else:
            partNumber = bootDev.partedPartition.number

        f = open(instRoot + cf, "w+")

        f.write("# yaboot.conf generated by anaconda\n\n")
        f.write("boot=%s\n" %(yabootTarget,))
        f.write("init-message=\"Welcome to %s!\\nHit <TAB> for boot options\"\n\n"
                % productName)

        f.write("partition=%s\n" % partNumber)
        f.write("timeout=%s\n" % (self.timeout or 80))
        f.write("install=/usr/lib/yaboot/yaboot\n")
        f.write("delay=5\n")
        f.write("enablecdboot\n")
        f.write("enableofboot\n")
        f.write("enablenetboot\n")        

        yabootProg = "/sbin/mkofboot"
        if iutil.getPPCMachine() == "PMac":
            # write out the first hfs/hfs+ partition as being macosx
            for (label, longlabel, device) in chainList:
                if ((not label) or (label == "")):
                    continue
                f.write("macosx=/dev/%s\n" %(device,))
                break
            
            f.write("magicboot=/usr/lib/yaboot/ofboot\n")

        elif iutil.getPPCMachine() == "pSeries":
            f.write("nonvram\n")
            f.write("fstype=raw\n")

        else: #  Default non-destructive case for anything else.
            f.write("nonvram\n")
            f.write("mntpoint=/boot/yaboot\n")
            f.write("usemount\n")
            if not os.access(instRoot + "/boot/yaboot", os.R_OK):
                os.mkdir(instRoot + "/boot/yaboot")
            yabootProg = "/sbin/ybin"

        if self.password:
            f.write("password=%s\n" %(self.password,))
            f.write("restricted\n")

        f.write("\n")

        rootDev = self.storage.rootDevice

        for (label, longlabel, version) in kernelList:
            kernelTag = "-" + version
            kernelFile = "%s/vmlinuz%s" %(cfPath, kernelTag)

            f.write("image=%s\n" %(kernelFile,))
            f.write("\tlabel=%s\n" %(label,))
            f.write("\tread-only\n")

            initrd = self.makeInitrd(kernelTag, instRoot)
            if initrd:
                f.write("\tinitrd=%s/%s\n" %(cfPath, initrd))

            append = "%s" %(self.args.get(),)

            realroot = rootDev.fstabSpec
            if rootIsDevice(realroot):
                f.write("\troot=%s\n" %(realroot,))
            else:
                if len(append) > 0:
                    append = "%s root=%s" %(append,realroot)
                else:
                    append = "root=%s" %(realroot,)

            if len(append) > 0:
                f.write("\tappend=\"%s\"\n" %(append,))
            f.write("\n")

        f.close()
        os.chmod(instRoot + cf, 0600)

        # FIXME: hack to make sure things are written to disk
        from pyanaconda import isys
        isys.sync()
        isys.sync()
        isys.sync()

        ybinargs = [ yabootProg, "-f", "-C", cf ]

        rc = iutil.execWithRedirect(ybinargs[0],
                                    ybinargs[1:],
                                    stdout = "/dev/tty5",
                                    stderr = "/dev/tty5",
                                    root = instRoot)
        if rc:
            return rc

        if (not os.access(instRoot + "/etc/yaboot.conf", os.R_OK) and
            os.access(instRoot + "/boot/etc/yaboot.conf", os.R_OK)):
            os.symlink("../boot/etc/yaboot.conf",
                       instRoot + "/etc/yaboot.conf")

        return 0

    def setPassword(self, val, isCrypted = 1):
        # yaboot just handles the password and doesn't care if its crypted
        # or not
        self.password = val

    def write(self, instRoot, bl, kernelList, chainList, defaultDev):
        if len(kernelList) >= 1:
            rc = self.writeYaboot(instRoot, bl, kernelList, 
                                  chainList, defaultDev)
            if rc:
                return rc
        else:
            raise BootyNoKernelWarning

        return 0

    def __init__(self, anaconda):
        bootloaderInfo.__init__(self, anaconda)
        self.useYabootVal = 1
        self.kernelLocation = "/boot"
        self._configdir = "/etc"
        self._configname = "yaboot.conf"
