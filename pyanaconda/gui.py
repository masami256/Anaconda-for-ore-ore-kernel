#
# gui.py - Graphical front end for anaconda
#
# Copyright (C) 1999, 2000, 2001, 2002, 2003, 2004, 2005, 2006, 2007
# Red Hat, Inc.  All rights reserved.
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
# Author(s): Matt Wilson <msw@redhat.com>
#            Michael Fulbright <msf@redhat.com>
#

import os
from flags import flags
os.environ["GNOME_DISABLE_CRASH_DIALOG"] = "1"

# we only want to enable the accessibility stuff if requested for now...
if flags.cmdline.has_key("dogtail"):
    os.environ["GTK_MODULES"] = "gail:atk-bridge"

import string
import time
import traceback
import isys
import iutil
import sys
import shutil
import gtk
import gtk.glade
import gobject
from language import expandLangs
from constants import *
from product import *
import network
from installinterfacebase import InstallInterfaceBase
import xutils
import imp
import iw

import gettext
_ = lambda x: gettext.ldgettext("anaconda", x)

import logging
log = logging.getLogger("anaconda")
stdout_log = logging.getLogger("anaconda.stdout")

isys.bind_textdomain_codeset("redhat-dist", "UTF-8")
iutil.setup_translations(gtk.glade)

class StayOnScreen(Exception):
    pass

mainWindow = None

stepToClass = {
    "kernelvm_top" : ("kernelvm_top_gui", "KernelvmTopWindow"),
    "language" : ("language_gui", "LanguageWindow"),
    "keyboard" : ("kbd_gui", "KeyboardWindow"),
    "filtertype" : ("filter_type", "FilterTypeWindow"),
    "filter" : ("filter_gui", "FilterWindow"), 
    "zfcpconfig" : ("zfcp_gui", "ZFCPWindow"),
    "partition" : ("partition_gui", "PartitionWindow"),
    "parttype" : ("autopart_type", "PartitionTypeWindow"),
    "cleardiskssel": ("cleardisks_gui", "ClearDisksWindow"),
    "findinstall" : ("examine_gui", "UpgradeExamineWindow"),
    "addswap" : ("upgrade_swap_gui", "UpgradeSwapWindow"),
    "upgrademigratefs" : ("upgrade_migratefs_gui", "UpgradeMigrateFSWindow"),
    "bootloader": ("bootloader_main_gui", "MainBootloaderWindow"),
    "upgbootloader": ("upgrade_bootloader_gui", "UpgradeBootloaderWindow"),
    "network" : ("network_gui", "NetworkWindow"),
    "timezone" : ("timezone_gui", "TimezoneWindow"),
    "accounts" : ("account_gui", "AccountWindow"),
    "tasksel": ("task_gui", "TaskWindow"),    
    "group-selection": ("package_gui", "GroupSelectionWindow"),
    "install" : ("progress_gui", "InstallProgressWindow"),
    "complete" : ("congrats_gui", "CongratulationWindow"),
}

if iutil.isS390():
    stepToClass["bootloader"] = ("zipl_gui", "ZiplWindow")

#
# Stuff for screenshots
#
screenshotDir = "/tmp/anaconda-screenshots"
screenshotIndex = 0

def copyScreenshots():
    # see if any screenshots taken
    if screenshotIndex == 0:
        return

    destDir = "/mnt/sysimage/root/anaconda-screenshots"
    if not os.access(destDir, os.R_OK):
        try:
            os.mkdir(destDir, 0750)
        except:
            window = MessageWindow("Error Saving Screenshot", 
                                   _("An error occurred saving screenshots "
                                     "to disk."), type="warning")
            return

    # Now copy all the PNGs over. Since some pictures could have been taken
    # under a root changed to /mnt/sysimage, we have to try to fetch files from
    # there as well.
    source_dirs = [screenshotDir, os.path.join("/mnt/sysimage", screenshotDir.lstrip('/'))]
    for source_dir in source_dirs:
        if not os.access(source_dir, os.X_OK):
            continue
        for f in os.listdir(source_dir):
            (path, fname) = os.path.split(f)
            (b, ext) = os.path.splitext(f)
            if ext == ".png":
                shutil.copyfile(source_dir + '/' + f, destDir + '/' + fname)

    window = MessageWindow(_("Screenshots Copied"), 
                           _("The screenshots have been saved in the "
                             "directory:\n\n"
                             "\t/root/anaconda-screenshots/\n\n"
                             "You can access these when you reboot and "
                             "login as root."))

def takeScreenShot():
    global screenshotIndex

    if not os.access(screenshotDir, os.R_OK):
        try:
            os.mkdir(screenshotDir)
        except OSError as e:
            log.error("os.mkdir() failed for %s: %s" % (screenshotDir, e.strerror))
            return

    try:
        screenshot = gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB, False, 8,
                     gtk.gdk.screen_width(), gtk.gdk.screen_height())
        screenshot.get_from_drawable(gtk.gdk.get_default_root_window(),
                                     gtk.gdk.colormap_get_system(),
                                     0, 0, 0, 0,
                                     gtk.gdk.screen_width(),
                                     gtk.gdk.screen_height())

        if screenshot:
            while (1):
                sname = "screenshot-%04d.png" % ( screenshotIndex,)
                if not os.access(screenshotDir + '/' + sname, os.R_OK):
                    break

                screenshotIndex += 1
                if screenshotIndex > 9999:
                    log.error("Too many screenshots!")
                    return

            screenshot.save (screenshotDir + '/' + sname, "png")
            screenshotIndex += 1

            window = MessageWindow(_("Saving Screenshot"), 
               _("A screenshot named '%s' has been saved.") % (sname,) ,
               type="ok")
    except:
        window = MessageWindow(_("Error Saving Screenshot"), 
                               _("An error occurred while saving "
                                 "the screenshot.  If this occurred "
                                 "during package installation, you may need "
                                 "to try several times for it to succeed."),
                               type="warning")

def handlePrintScrnRelease (window, event):
    if event.keyval == gtk.keysyms.Print:
        takeScreenShot()
#
# HACK to make treeview work
# 
 
def setupTreeViewFixupIdleHandler(view, store):
    id = {}
    id["id"] = gobject.idle_add(scrollToIdleHandler, (view, store, id))

def scrollToIdleHandler((view, store, iddict)):
    if not view or not store or not iddict:
        return

    try:
        id = iddict["id"]
    except:
        return
    
    selection = view.get_selection()
    if not selection:
        return
    
    model, iter = selection.get_selected()
    if not iter:
        return

    path = store.get_path(iter)
    col = view.get_column(0)
    view.scroll_to_cell(path, col, True, 0.5, 0.5)

    if id:
        gobject.source_remove(id)

# setup globals
def processEvents():
    gtk.gdk.flush()
    while gtk.events_pending():
        gtk.main_iteration(False)

def widgetExpander(widget, growTo=None):
    widget.connect("size-allocate", growToParent, growTo)

def growToParent(widget, rect, growTo=None):
    if not widget.parent:
        return
    ignore = widget.__dict__.get("ignoreEvents")
    if not ignore:
        if growTo:
            x, y, width, height = growTo.get_allocation()
            widget.set_size_request(width, -1)
        else:
            widget.set_size_request(rect.width, -1)
        widget.ignoreEvents = 1
    else:
        widget.ignoreEvents = 0

_busyCursor = 0

def setCursorToBusy(process=1):
    root = gtk.gdk.get_default_root_window()
    cursor = gtk.gdk.Cursor(gtk.gdk.WATCH)
    root.set_cursor(cursor)
    if process:
        processEvents()

def setCursorToNormal():
    root = gtk.gdk.get_default_root_window()
    cursor = gtk.gdk.Cursor(gtk.gdk.LEFT_PTR)
    root.set_cursor(cursor)

def rootPushBusyCursor(process=1):
    global _busyCursor
    _busyCursor += 1
    if _busyCursor > 0:
        setCursorToBusy(process)

def rootPopBusyCursor():
    global _busyCursor
    _busyCursor -= 1
    if _busyCursor <= 0:
        setCursorToNormal()

def getBusyCursorStatus():
    global _busyCursor
    
    return _busyCursor

class MnemonicLabel(gtk.Label):
    def __init__(self, text="", alignment = None):
        gtk.Label.__init__(self, "")
        self.set_text_with_mnemonic(text)
        if alignment is not None:
            apply(self.set_alignment, alignment)

class WrappingLabel(gtk.Label):
    def __init__(self, label=""):
        gtk.Label.__init__(self, label)
        self.set_line_wrap(True)
        self.ignoreEvents = 0
        widgetExpander(self)

def titleBarMousePressCB(widget, event, data):
    if event.type & gtk.gdk.BUTTON_PRESS:
        (x, y) = data["window"].get_position()
        data["state"] = 1
        data["button"] = event.button
        data["deltax"] = event.x_root - x
        data["deltay"] = event.y_root - y
    
def titleBarMouseReleaseCB(widget, event, data):
    if data["state"] and event.button == data["button"]:
        data["state"] = 0
        data["button"] = 0
        data["deltax"] = 0
        data["deltay"] = 0

def titleBarMotionEventCB(widget, event, data):
    if data["state"]:
        newx = event.x_root - data["deltax"]
        newy = event.y_root - data["deltay"]
        if newx < 0:
            newx = 0
        if newy < 0:
            newy = 0
        (w, h) = data["window"].get_size()
        if (newx+w) > gtk.gdk.screen_width():
            newx = gtk.gdk.screen_width() - w
        if (newy+20) > (gtk.gdk.screen_height()):
            newy = gtk.gdk.screen_height() - 20

        data["window"].move(int(newx), int(newy))

def addFrame(dialog, title=None):
    # make screen shots work
    dialog.connect ("key-release-event", handlePrintScrnRelease)
    if title:
        dialog.set_title(title)

def findGladeFile(file):
    path = os.environ.get("GLADEPATH", "./:ui/:/tmp/updates/:/tmp/updates/ui/")
    for dir in path.split(":"):
        fn = dir + file
        if os.access(fn, os.R_OK):
            return fn
    raise RuntimeError, "Unable to find glade file %s" % file

def getGladeWidget(file, rootwidget, i18ndomain="anaconda"):
    f = findGladeFile(file)
    xml = gtk.glade.XML(f, root = rootwidget, domain = i18ndomain)
    w = xml.get_widget(rootwidget)
    if w is None:
        raise RuntimeError, "Unable to find root widget %s in %s" %(rootwidget, file)

    return (xml, w)

def findPixmap(file):
    path = os.environ.get("PIXMAPPATH", "./:pixmaps/:/tmp/updates/:/tmp/updates/pixmaps/")
    for dir in path.split(":"):
        fn = dir + file
        if os.access(fn, os.R_OK):
            return fn
    return None

def getPixbuf(file):
    fn = findPixmap(file)
    if not fn:
        log.error("unable to load %s" %(file,))
        return None

    try:
        pixbuf = gtk.gdk.pixbuf_new_from_file(fn)
    except RuntimeError, msg:
        log.error("unable to read %s: %s" %(file, msg))
        pixbuf = None
    
    return pixbuf

def readImageFromFile(file, dither = False, image = None):
    pixbuf = getPixbuf(file)
    if pixbuf is None:
        log.warning("can't find pixmap %s" %(file,))
        return None

    if image is None:
        p = gtk.Image()
    else:
        p = image
    if dither:
        (pixmap, mask) = pixbuf.render_pixmap_and_mask()
        pixmap.draw_pixbuf(gtk.gdk.GC(pixmap), pixbuf, 0, 0, 0, 0,
                           pixbuf.get_width(), pixbuf.get_height(),
                           gtk.gdk.RGB_DITHER_MAX, 0, 0)
        p = gtk.Image()
        p.set_from_pixmap(pixmap, mask)
    else:
        source = gtk.IconSource()
        source.set_pixbuf(pixbuf)
        source.set_size(gtk.ICON_SIZE_DIALOG)
        source.set_size_wildcarded(False)
        iconset = gtk.IconSet()
        iconset.add_source(source)
        p.set_from_icon_set(iconset, gtk.ICON_SIZE_DIALOG)

    return p

class WaitWindow:
    def __init__(self, title, text, parent = None):
        if flags.livecdInstall:
            self.window = gtk.Window()
            if parent:
                self.window.set_transient_for(parent)
        else:
            self.window = gtk.Window()
        self.window.set_modal(True)
        self.window.set_type_hint (gtk.gdk.WINDOW_TYPE_HINT_DIALOG)
        self.window.set_title(title)
        self.window.set_position(gtk.WIN_POS_CENTER)
        label = WrappingLabel(text)
        box = gtk.Frame()
        box.set_border_width(10)
        box.add(label)
        box.set_shadow_type(gtk.SHADOW_NONE)
        self.window.add(box)
        box.show_all()
        addFrame(self.window)
        # Displaying windows should not be done outside of the gtk
        # mainloop. With metacity this bites us and we have to do
        # window.show_now() AND refresh() to correctly display the window and
        # its contents:
        self.window.show_now()
        rootPushBusyCursor()
        self.refresh()

    def refresh(self):
        processEvents()
            
    def pop(self):
        self.window.destroy()
        rootPopBusyCursor()

class ProgressWindow:
    def __init__(self, title, text, total, updpct = 0.05, updsecs=10,
                 parent = None, pulse = False):
        if flags.livecdInstall:
            self.window = gtk.Window()
            if parent:
                self.window.set_transient_for(parent)
        else:
            self.window = gtk.Window()

        self.window.set_modal(True)
        self.window.set_type_hint (gtk.gdk.WINDOW_TYPE_HINT_DIALOG)
        self.window.set_title (title)
        self.window.set_position (gtk.WIN_POS_CENTER)
        self.lastUpdate = time.time()
        self.updsecs = updsecs
        box = gtk.VBox (False, 5)
        box.set_border_width (10)

        label = WrappingLabel (text)
        label.set_alignment (0.0, 0.5)
        box.pack_start (label, False)

        self.total = total
        self.updpct = updpct
        self.progress = gtk.ProgressBar ()
        box.pack_start (self.progress, True)
        box.show_all()
        self.window.add(box)
        addFrame(self.window)
        # see comment at WaitWindow.__init__():
        self.window.show_now ()
        rootPushBusyCursor()
        self.refresh()

    def refresh(self):
        processEvents()

    def pulse(self):
        then = self.lastUpdate
        now = time.time()
        delta = now-then
        if delta < 0.01:
            return
        self.progress.set_pulse_step(self.updpct)
        self.lastUpdate = now
        # if we've had a largish gap, some smoothing does actually help,
        # but don't go crazy
        if delta > 2:
            delta=2
        while delta > 0:
            self.progress.pulse()
            processEvents()
            delta -= 0.05

    def set (self, amount):
        amount = min(amount, self.total)

        # only update widget if we've changed by 5% or our timeout has
        # expired
        curval = self.progress.get_fraction()
        newval = float (amount) / self.total
        then = self.lastUpdate
        now = time.time()
        if newval < 0.998:
            if ((newval - curval) < self.updpct and (now-then) < self.updsecs):
                return
        self.lastUpdate = now
        self.progress.set_fraction (newval)
        processEvents ()

    def pop(self):
        self.window.destroy ()
        rootPopBusyCursor()

class luksPassphraseWindow:
    def __init__(self, passphrase=None, preexist = False, parent = None):
        luksxml = gtk.glade.XML(findGladeFile("lukspassphrase.glade"),
                                domain="anaconda",
                                root="luksPassphraseDialog")
        self.passphraseEntry = luksxml.get_widget("passphraseEntry")
        self.passphraseEntry.set_visibility(False)
        self.confirmEntry = luksxml.get_widget("confirmEntry")
        self.confirmEntry.set_visibility(False)
        self.win = luksxml.get_widget("luksPassphraseDialog")
        self.okButton = luksxml.get_widget("okbutton1")
        self.globalcheckbutton = luksxml.get_widget("globalcheckbutton")

        self.isglobal = preexist
        if not preexist:
            self.globalcheckbutton.hide()
        else:
            self.globalcheckbutton.set_active(True)

        self.minimumLength = 8  # arbitrary; should probably be much larger
        if passphrase:
            self.initialPassphrase = passphrase
            self.passphraseEntry.set_text(passphrase)
            self.confirmEntry.set_text(passphrase)
        else:
            self.initialPassphrase = ""

        txt = _("Choose a passphrase for the encrypted devices. "
                "You will be prompted for this passphrase during system "
                "boot.")
        luksxml.get_widget("mainLabel").set_text(txt)

        if parent:
            self.win.set_transient_for(parent)

        addFrame(self.win)

    def run(self):
        self.win.show()
        while True:
            self.passphraseEntry.grab_focus()
            self.rc = self.win.run()
            if self.rc == gtk.RESPONSE_OK:
                passphrase = self.passphraseEntry.get_text()
                confirm = self.confirmEntry.get_text()
                if passphrase != confirm:
                    MessageWindow(_("Error with passphrase"),
                                  _("The passphrases you entered were "
                                    "different.  Please try again."),
                                  type = "ok", custom_icon = "error")
                    self.confirmEntry.set_text("")
                    continue

                if len(passphrase) < self.minimumLength:
                    MessageWindow(_("Error with passphrase"),
                                    _("The passphrase must be at least "
                                      "eight characters long."),
                                  type = "ok", custom_icon = "error")
                    self.passphraseEntry.set_text("")
                    self.confirmEntry.set_text("")
                    continue

                if self.isglobal:
                    self.isglobal = self.globalcheckbutton.get_active()
            else:
                self.passphraseEntry.set_text(self.initialPassphrase)
                self.confirmEntry.set_text(self.initialPassphrase)

            return self.rc

    def getPassphrase(self):
        return self.passphraseEntry.get_text()

    def getGlobal(self):
        return self.isglobal

    def getrc(self):
        return self.rc

    def destroy(self):
        self.win.destroy()

class PassphraseEntryWindow:
    def __init__(self, device, parent = None):
        def ok(*args):
            self.win.response(gtk.RESPONSE_OK)
        xml = gtk.glade.XML(findGladeFile("lukspassphrase.glade"),
                            domain="anaconda",
                            root="passphraseEntryDialog")
        self.txt = _("Device %s is encrypted. In order to "
                     "access the device's contents during "
                     "installation you must enter the device's "
                     "passphrase below.") % (device,)
        self.win = xml.get_widget("passphraseEntryDialog")
        self.passphraseLabel = xml.get_widget("passphraseLabel")
        self.passphraseEntry = xml.get_widget("passphraseEntry2")
        self.globalcheckbutton = xml.get_widget("globalcheckbutton")

        if parent:
            self.win.set_transient_for(parent)

        self.passphraseEntry.connect('activate', ok)
        addFrame(self.win)

    def run(self):
        self.win.show()
        self.passphraseLabel.set_text(self.txt)
        self.passphraseEntry.grab_focus()

        busycursor = getBusyCursorStatus()
        setCursorToNormal()

        rc = self.win.run()
        passphrase = None
        isglobal = False
        if rc == gtk.RESPONSE_OK:
            passphrase = self.passphraseEntry.get_text()
            isglobal = self.globalcheckbutton.get_active()

        if busycursor:
            setCursorToBusy()

        self.rc = (passphrase, isglobal)
        return self.rc

    def getrc(self):
        return self.rc

    def destroy(self):
        self.win.destroy()

class MessageWindow:
    def getrc (self):
        return self.rc

    def __init__ (self, title, text, type="ok", default=None, custom_buttons=None, custom_icon=None, run = True, parent = None, destroyAfterRun = True):
        self.debugRid = None
        self.title = title
        if flags.autostep:
            self.rc = 1
            return
        self.rc = None
        self.framed = False
        self.doCustom = False

        style = 0
        if type == 'ok':
            buttons = gtk.BUTTONS_OK
            style = gtk.MESSAGE_INFO
        elif type == 'warning':
            buttons = gtk.BUTTONS_OK
            style = gtk.MESSAGE_WARNING
        elif type == 'okcancel':
            buttons = gtk.BUTTONS_OK_CANCEL
            style = gtk.MESSAGE_WARNING
        elif type == 'yesno':
            buttons = gtk.BUTTONS_YES_NO
            style = gtk.MESSAGE_QUESTION
        elif type == 'custom':
            self.doCustom = True
            buttons = gtk.BUTTONS_NONE
            style = gtk.MESSAGE_QUESTION

        if custom_icon == "warning":
            style = gtk.MESSAGE_WARNING
        elif custom_icon == "question":
            style = gtk.MESSAGE_QUESTION
        elif custom_icon == "error":
            style = gtk.MESSAGE_ERROR
        elif custom_icon == "info":
            style = gtk.MESSAGE_INFO

        self.dialog = gtk.MessageDialog(mainWindow, 0, style, buttons, str(text))
        self.dialog.set_property("use-markup", True)

        if parent:
            self.dialog.set_transient_for(parent)

        if self.doCustom:
            rid=0
            for button in custom_buttons:
                if button == _("Cancel"):
                    tbutton = "gtk-cancel"
                else:
                    tbutton = button

                widget = self.dialog.add_button(tbutton, rid)
                rid = rid + 1

            if default is not None:
                defaultchoice = default
            else:
                defaultchoice = rid - 1
            if flags.debug and not _("_Debug") in custom_buttons:
                widget = self.dialog.add_button(_("_Debug"), rid)
                self.debugRid = rid
                rid += 1

        else:
            if default == "no":
                defaultchoice = 0
            elif default == "yes" or default == "ok":
                defaultchoice = 1
            else:
                defaultchoice = 0

        self.dialog.set_position (gtk.WIN_POS_CENTER)
        self.dialog.set_default_response(defaultchoice)
        if run:
            self.run(destroyAfterRun)

    def run(self, destroy = False):
        if not self.framed:
            addFrame(self.dialog, title=self.title)
            self.framed = True
        self.dialog.show_all ()

        # XXX - Messy - turn off busy cursor if necessary
        busycursor = getBusyCursorStatus()
        setCursorToNormal()
        self.rc = self.dialog.run()

        if not self.doCustom:
            if self.rc in [gtk.RESPONSE_OK, gtk.RESPONSE_YES]:
                self.rc = 1
            elif self.rc in [gtk.RESPONSE_CANCEL, gtk.RESPONSE_NO,
                             gtk.RESPONSE_CLOSE, gtk.RESPONSE_DELETE_EVENT]:
                self.rc = 0
        else:
            # generated by Esc key
            if self.rc == gtk.RESPONSE_DELETE_EVENT:
                self.rc = 0

        if not self.debugRid is None and self.rc == self.debugRid:
            self.debugClicked(self)
            return self.run(destroy)

        if destroy:
            self.dialog.destroy()

        # restore busy cursor
        if busycursor:
            setCursorToBusy()

    def debugClicked (self, *args):
        try:
            # switch to VC1 so we can debug
            isys.vtActivate (1)
        except SystemError:
            pass
        import pdb
        try:
            pdb.set_trace()
        except:
            sys.exit(-1)
        try:
            # switch back
            isys.vtActivate (6)
        except SystemError:
            pass

class ReinitializeWindow(MessageWindow):

    def __init__ (self, title, path, size, description,
                  default=None, run=True, parent=None, destroyAfterRun=True):

        self.debugRid = None
        self.title = title
        if flags.autostep:
            self.rc = 1
            return
        self.rc = None
        self.framed = False
        self.doCustom = False

        xml = gtk.glade.XML(findGladeFile("reinitialize-dialog.glade"),
                            domain="anaconda")

        self.dialog = xml.get_widget("reinitializeDialog")
        self.apply_to_all = xml.get_widget("apply_to_all")

        self.label = xml.get_widget("disk_label")
        text = "<b>%s</b>\n%s MB\t%s" % (description, size, path)
        self.label.set_markup(text)

        if parent:
            self.dialog.set_transient_for(parent)
        self.dialog.set_position(gtk.WIN_POS_CENTER)

        if flags.debug:
            widget = self.dialog.add_button(_("_Debug"), 2)
            self.debugRid = 2

        defaultchoice = 0 #no
        self.dialog.set_default_response(defaultchoice)

        if run:
            self.run(destroyAfterRun)

    def run(self, destroy=False):
        MessageWindow.run(self, destroy)
        apply_all = self.apply_to_all.get_active()

        # doCustom is false, so we will have self.rc set up as following:
        # if "Yes, discard" was clicked - self.rc = 1
        # if "No, keep" was clicked     - self.rc = 0
        if self.rc == 1: #yes
            self.rc = 3 if apply_all else 2
        elif self.rc == 0: #no
            self.rc = 1 if apply_all else 0

class DetailedMessageWindow(MessageWindow):
    def __init__(self, title, text, longText=None, type="ok", default=None, custom_buttons=None, custom_icon=None, run=True, parent=None, destroyAfterRun=True, expanded=False):
        self.title = title

        if flags.autostep:
            self.rc = 1
            return

        self.debugRid = None
        self.rc = None
        self.framed = False
        self.doCustom = False

        if type == 'ok':
            buttons = ["gtk-ok"]
        elif type == 'warning':
            buttons = ["gtk-ok"]
        elif type == 'okcancel':
            buttons = ["gtk-ok", "gtk-cancel"]
        elif type == 'yesno':
            buttons = ["gtk-yes", "gtk-no"]
        elif type == 'custom':
            self.doCustom = True
            buttons = custom_buttons

        xml = gtk.glade.XML(findGladeFile("detailed-dialog.glade"), domain="anaconda")
        self.dialog = xml.get_widget("detailedDialog")
        self.mainVBox = xml.get_widget("mainVBox")
        self.hbox = xml.get_widget("hbox1")
        self.info = xml.get_widget("info")
        self.detailedExpander = xml.get_widget("detailedExpander")
        self.detailedView = xml.get_widget("detailedView")

        self.detailedExpander.set_expanded(expanded)

        if parent:
            self.dialog.set_transient_for(parent)

        if custom_icon:
            img = gtk.Image()
            img.set_from_file(custom_icon)
            self.hbox.pack_start(img)
            self.hbox.reorder_child(img, 0)

        rid = 0
        for button in buttons:
            self.dialog.add_button(button, rid)
            rid += 1

        if self.doCustom:
            defaultchoice = rid-1
            if flags.debug and not _("_Debug") in buttons:
                self.dialog.add_button(_("_Debug"), rid)
                self.debugRid = rid
                rid += 1
        else:
            if default == "no":
                defaultchoice = 0
            elif default == "yes" or default == "ok":
                defaultchoice = 1
            else:
                defaultchoice = 0

        self.info.set_text(text)

        if longText:
            textbuf = gtk.TextBuffer()
            iter = textbuf.get_start_iter()

            for line in longText:
                if __builtins__.get("type")(line) != unicode:
                    try:
                        line = unicode(line, encoding='utf-8')
                    except UnicodeDecodeError, e:
                        log.error("UnicodeDecodeException: line = %s" % (line,))
                        log.error("UnicodeDecodeException: %s" % (str(e),))

                textbuf.insert(iter, line)

            self.detailedView.set_buffer(textbuf)
        else:
            self.mainVBox.remove(self.detailedExpander)

        self.dialog.set_position (gtk.WIN_POS_CENTER)
        self.dialog.set_default_response(defaultchoice)

        if run:
            self.run(destroyAfterRun)

class EntryWindow(MessageWindow):
    def __init__ (self, title, text, prompt, entrylength = None):
        mainWindow = None
        MessageWindow.__init__(self, title, text, type = "okcancel", custom_icon="question", run = False)
        self.entry = gtk.Entry()
        if entrylength:
            self.entry.set_width_chars(entrylength)
            self.entry.set_max_length(entrylength)

        # eww, eww, eww... but if we pack in the vbox, it goes to the right
        # place!
        self.dialog.child.pack_start(self.entry)

    def run(self):
        MessageWindow.run(self)
        if self.rc == 0:
            return None
        t = self.entry.get_text()
        t.strip()
        if len(t) == 0:
            return None
        return t

    def destroy(self):
        self.dialog.destroy()

class InstallInterface(InstallInterfaceBase):
    def __init__ (self):
        InstallInterfaceBase.__init__(self)
        self.icw = None
        self.installProgress = None

        root = gtk.gdk.get_default_root_window()
        cursor = gtk.gdk.Cursor(gtk.gdk.LEFT_PTR)
        root.set_cursor(cursor)

    def __del__ (self):
        pass

    def shutdown (self):
        pass

    def suspend(self):
        pass
    
    def resume(self):
        pass


    # just_setup is used for [Configure Network] button
    def enableNetwork(self, just_setup=False):

        if len(self.anaconda.network.netdevices) == 0:
            return False


        from iw.network_gui import (runNMCE,
                                 selectInstallNetDeviceDialog,
                                 selectSSIDsDialog)

        networkEnabled = False
        while not networkEnabled:

            # We need to do it in each iteration because user can
            # delete ifcfg file in nm-c-e
            nm_controlled_devices = [devname for (devname, dev)
                                     in self.anaconda.network.netdevices.items()
                                     if not dev.usedByFCoE(self.anaconda)]
            if not just_setup and not nm_controlled_devices:
                return False

            if just_setup:
                install_device = None
            else:
                install_device = selectInstallNetDeviceDialog(self.anaconda.network,
                                                              nm_controlled_devices)
                if not install_device:
                    break

            # update ifcfg files for nm-c-e
            self.anaconda.network.setNMControlledDevices(nm_controlled_devices)
            if not just_setup:
                self.anaconda.network.presetDefaultConfiguration([install_device])

            # we might want to do this only once
            if self.anaconda.network.hasWirelessDev():
                # NOTE: For wireless, we need supplicant to go to ready state,
                #       that means to get the wireless device managed by NM
                self.anaconda.network.writeIfcfgFiles()
                w = self.anaconda.intf.waitWindow(_("Wireless setup"),
                                    _("Scanning access points for wireless devices"))
                # get available wireless APs
                dev_all_ssids = self.anaconda.network.getSSIDs()
                w.pop()
                # prefer APs we already have set e.g. via kickstart or stage 1
                self.anaconda.network.selectPreferredSSIDs(dev_all_ssids)
                # select wireless APs
                dev_ssids = selectSSIDsDialog(dev_all_ssids) or dev_all_ssids
                self.anaconda.network.updateIfcfgsSSID(dev_ssids)

            self.anaconda.network.writeIfcfgFiles()
            # Logging can race here with ifcfg-rh updating the file
            network.logIfcfgFiles(message="Dump before nm-c-e (can race "
                                           "with ifcfg updating). ")
            runNMCE(self.anaconda)
            network.logIfcfgFiles(message="Dump after nm-c-e. ")

            self.anaconda.network.update()

            if just_setup:
                waited_devs = self.anaconda.network.getOnbootControlledIfaces()
            else:
                waited_devs = [install_device]
                self.anaconda.network.updateActiveDevices([install_device])

            self.anaconda.network.write()

            if waited_devs:
                w = WaitWindow(_("Waiting for NetworkManager"),
                               _("Waiting for NetworkManager to activate "
                                 "these devices: %s") % ",".join(waited_devs))
                failed_devs = self.anaconda.network.waitForDevicesActivation(waited_devs)
                w.pop()

                if just_setup:
                    if failed_devs:
                        self._handleDeviceActivationFail(failed_devs)
                    if len(failed_devs) < len(waited_devs):
                        # if any device was activated, remember to reset
                        # resolver
                        networkEnabled = True
                else:
                    networkEnabled = install_device not in failed_devs
                    if not networkEnabled:
                        self._handleNetworkError(install_device)

            if just_setup:
                break

        if networkEnabled:
            network.resetResolver()
        return networkEnabled

    def _handleDeviceActivationFail(self, devices):
        d = gtk.MessageDialog(None, 0, gtk.MESSAGE_ERROR,
                              gtk.BUTTONS_OK,
                              _("Failed to activate these "
                                "network interfaces: %s" %
                                ",".join(devices)))
        d.set_title(_("Network Configuration"))
        d.set_position(gtk.WIN_POS_CENTER)
        addFrame(d)
        d.run()
        d.destroy()

    def _handleNetworkError(self, field):
        d = gtk.MessageDialog(None, 0, gtk.MESSAGE_ERROR,
                              gtk.BUTTONS_OK,
                              _("An error occurred trying to bring up the "
                                "%s network interface.") % (field,))
        d.set_title(_("Error Enabling Network"))
        d.set_position(gtk.WIN_POS_CENTER)
        addFrame(d)
        d.run()
        d.destroy()

    def setInstallProgressClass(self, c):
        self.instProgress = c

    def setPackageProgressWindow (self, ppw):
        self.ppw = ppw

    def waitWindow (self, title, text):
        if self.icw:
            return WaitWindow (title, text, self.icw.window)
        else:
            return WaitWindow (title, text)

    def progressWindow (self, title, text, total, updpct = 0.05, pulse = False):
        if self.icw:
            return ProgressWindow (title, text, total, updpct,
                                   parent = self.icw.window, pulse = pulse)
        else:
            return ProgressWindow (title, text, total, updpct, pulse = pulse)

    def messageWindow(self, title, text, type="ok", default = None,
             custom_buttons=None,  custom_icon=None):
        if self.icw:
            parent = self.icw.window
        else:
            parent = None

        rc = MessageWindow (title, text, type, default,
                custom_buttons, custom_icon, run=True, parent=parent).getrc()
        return rc

    def reinitializeWindow(self, title, path, size, description):
        if self.icw:
            parent = self.icw.window
        else:
            parent = None

        rc = ReinitializeWindow(title, path, size, description, parent=parent).getrc()
        return rc

    def createRepoWindow(self):
        from iw.task_gui import RepoCreator
        dialog = RepoCreator(self.anaconda)
        dialog.createDialog()
        dialog.run()

    def editRepoWindow(self, repoObj):
        from iw.task_gui import RepoEditor
        dialog = RepoEditor(self.anaconda, repoObj)
        dialog.createDialog()
        dialog.run()

    def methodstrRepoWindow(self, methodstr):
        from iw.task_gui import RepoMethodstrEditor
        dialog = RepoMethodstrEditor(self.anaconda, methodstr)
        dialog.createDialog()
        return dialog.run()

    def entryWindow(self, title, text, type="ok", entrylength = None):
        d = EntryWindow(title, text, type, entrylength)
        rc = d.run()
        d.destroy()
        return rc

    def detailedMessageWindow(self, title, text, longText=None, type="ok",
                              default=None, custom_buttons=None,
                              custom_icon=None, expanded=False):
        if self.icw:
            parent = self.icw.window
        else:
            parent = None

        rc = DetailedMessageWindow (title, text, longText, type, default,
                                    custom_buttons, custom_icon, run=True,
                                    parent=parent, expanded=expanded).getrc()
        return rc

    def mainExceptionWindow(self, shortText, longTextFile):
        from meh.ui.gui import MainExceptionWindow
        log.critical(shortText)
        win = MainExceptionWindow (shortText, longTextFile)
        addFrame(win.dialog)
        return win

    def saveExceptionWindow(self, accountManager, signature):
        from meh.ui.gui import SaveExceptionWindow
        import urlgrabber

        if not network.hasActiveNetDev():
            if self.messageWindow(_("Warning"),
                   _("You do not have an active network connection.  This is "
                     "required by some exception saving methods.  Would you "
                     "like to configure your network now?"),
                   type = "yesno"):

                if not self.enableNetwork():
                    self.messageWindow(_("No Network Available"),
                                       _("Remote exception saving methods will not work."))
                else:
                    urlgrabber.grabber.reset_curl_obj()

        win = SaveExceptionWindow (accountManager, signature)
        win.run()

    def exitWindow(self, title, text):
        if self.icw:
            parent = self.icw.window
        else:
            parent = None

        rc = MessageWindow (title, text, type="custom",
                            custom_icon="info", parent=parent,
                            custom_buttons=[_("_Exit installer")]).getrc()
        return rc

    def getLuksPassphrase(self, passphrase = "", preexist = False):
        if self.icw:
            parent = self.icw.window
        else:
            parent = None

        d = luksPassphraseWindow(passphrase, parent = parent,
                                 preexist = preexist)
        rc = d.run()
        passphrase = d.getPassphrase()
        isglobal = d.getGlobal()
        d.destroy()
        return (passphrase, isglobal)

    def passphraseEntryWindow(self, device):
        if self.icw:
            parent = self.icw.window
        else:
            parent = None

        d = PassphraseEntryWindow(device, parent = parent)
        rc = d.run()
        d.destroy()
        return rc

    def beep(self):
        gtk.gdk.beep()

    def kickstartErrorWindow(self, text):
        s = _("The following error was found while parsing the "
              "kickstart configuration file:\n\n%s") %(text,)
        return self.messageWindow(_("Error Parsing Kickstart Config"),
                                  s,
                                  type = "custom",
                                  custom_buttons = [_("_Exit installer")],
                                  custom_icon = "error")

    def getBootdisk (self):
        return None

    def run(self, anaconda):
        self.anaconda = anaconda

        if anaconda.keyboard and not flags.livecdInstall:
            anaconda.keyboard.activate()

        self.icw = InstallControlWindow (self.anaconda)
        self.icw.run()

    def setSteps(self, anaconda):
        pass

class InstallControlWindow:
    def setLanguage (self):
        if not self.__dict__.has_key('window'): return
        self.reloadRcQueued = 1
        self.setup_window(True)

    def setLtR(self):
        ltrrtl = gettext.dgettext("gtk20", "default:LTR")
        if ltrrtl == "default:RTL":
            gtk.widget_set_default_direction (gtk.TEXT_DIR_RTL)
        elif ltrrtl == "default:LTR":
            gtk.widget_set_default_direction (gtk.TEXT_DIR_LTR)
        else:
            log.error("someone didn't translate the ltr bits right: %s" %(ltrrtl,))
            gtk.widget_set_default_direction (gtk.TEXT_DIR_LTR)            
        
    def prevClicked (self, *args):
        try:
            self.currentWindow.getPrev ()
        except StayOnScreen:
            return

        self.anaconda.dispatch.gotoPrev()
        self.setScreen ()

    def nextClicked (self, *args):
        try:
            rc = self.currentWindow.getNext ()
        except StayOnScreen:
            return

        self.anaconda.dispatch.gotoNext()
        self.setScreen ()

    def debugClicked (self, *args):
        try:
            # switch to VC1 so we can debug
            isys.vtActivate (1)
        except SystemError:
            pass
        import pdb
        try:
            pdb.set_trace()
        except:
            sys.exit(-1)
        try:
            # switch back
            isys.vtActivate (6)
        except SystemError:
            pass

    def handleRenderCallback(self):
        self.currentWindow.renderCallback()
        if flags.autostep:
            if flags.autoscreenshot:
                # let things settle down graphically
                processEvents()
                time.sleep(1)
                takeScreenShot()
            self.nextClicked()
        else:
            gobject.source_remove(self.handle)

    def setScreen (self):
        (step, anaconda) = self.anaconda.dispatch.currentStep()
        if step is None:
            gtk.main_quit()
            return

        if not stepToClass[step]:
            if self.anaconda.dispatch.dir == DISPATCH_FORWARD:
                return self.nextClicked()
            else:
                return self.prevClicked()

        (file, className) = stepToClass[step]
        newScreenClass = None

        while True:
            try:
                found = imp.find_module(file, iw.__path__)
                moduleName = 'pyanaconda.iw.%s' % file
                loaded = imp.load_module(moduleName, *found)
                newScreenClass = loaded.__dict__[className]
                break
            except ImportError, e:
                stdout_log.error("loading interface component %s" % className)
                stdout_log.error(traceback.format_exc())
                win = MessageWindow(_("Error!"),
                                    _("An error occurred when attempting "
                                      "to load an installer interface "
                                      "component.\n\nclassName = %s")
                                    % (className,),
                                    type="custom", custom_icon="warning",
                                    custom_buttons=[_("_Exit"),
                                                    _("_Retry")])
                if not win.getrc():
                    msg =  _("The system will now reboot.")
                    buttons = [_("_Reboot")]

                    MessageWindow(_("Exiting"),
                                  msg,
                                  type="custom",
                                  custom_icon="warning",
                                  custom_buttons=buttons)
                    sys.exit(0)

        ics = InstallControlState (self)
        ics.setPrevEnabled(self.anaconda.dispatch.canGoBack())
        self.destroyCurrentWindow()
        self.currentWindow = newScreenClass(ics)

        new_screen = self.currentWindow.getScreen(anaconda)

        # If the getScreen method returned None, that means the screen did not
        # want to be displayed for some reason and we should skip to the next
        # step.  However, we do not want to remove the current step from the
        # list as later events may cause the screen to be displayed.
        if not new_screen:
            if self.anaconda.dispatch.dir == DISPATCH_FORWARD:
                self.anaconda.dispatch.gotoNext()
            else:
                self.anaconda.dispatch.gotoPrev()

            return self.setScreen()

        self.update (ics)

        self.installFrame.add(new_screen)
        self.installFrame.show_all()

        self.currentWindow.focus()

        self.handle = gobject.idle_add(self.handleRenderCallback)

        if self.reloadRcQueued:
            self.window.reset_rc_styles()
            self.reloadRcQueued = 0

    def destroyCurrentWindow(self):
        children = self.installFrame.get_children ()
        if children:
            child = children[0]
            self.installFrame.remove (child)
            child.destroy ()
        self.currentWindow = None

    def update (self, ics):
        self.mainxml.get_widget("backButton").set_sensitive(ics.getPrevEnabled())
        self.mainxml.get_widget("nextButton").set_sensitive(ics.getNextEnabled())

        if ics.getGrabNext():
            self.mainxml.get_widget("nextButton").grab_focus()

        self.mainxml.get_widget("nextButton").set_flags(gtk.HAS_DEFAULT)

    def __init__ (self, anaconda):
        self.reloadRcQueued = 0
        self.currentWindow = None
        self.anaconda = anaconda
        self.handle = None
        self.window = None

    def keyRelease (self, window, event):
        if ((event.keyval == gtk.keysyms.KP_Delete
             or event.keyval == gtk.keysyms.Delete)
            and (event.state & (gtk.gdk.CONTROL_MASK | gtk.gdk.MOD1_MASK))):
            self._doExit()
        # XXX hack: remove me when the accelerators work again.
        elif (event.keyval == gtk.keysyms.F12
              and self.currentWindow.getICS().getNextEnabled()):
            self.nextClicked()
        elif event.keyval == gtk.keysyms.Print:
            takeScreenShot()

    def _doExit (self, *args):
        gtk.main_quit()
        os._exit(0)

    def _doExitConfirm (self, win = None, *args):
        # FIXME: translate the string
        win = MessageWindow(_("Exit installer"),
                            _("Are you sure you wish to exit the installer?"),
                            type="custom", custom_icon="question",
                            custom_buttons = [_("Cancel"), _("_Exit installer")],
                            parent = win)
        if win.getrc() == 0:
            return True
        self._doExit()

    def createWidgets (self):
        self.window.set_title(_("%s Installer") %(productName,))

        i = self.mainxml.get_widget("headerImage")
        p = readImageFromFile("anaconda_header.png",
                              dither = False, image = i)
        if p is None:
            print(_("Unable to load title bar"))

        if flags.livecdInstall:
            i.hide()
            self.window.set_resizable(True)
            self.window.maximize()
        elif flags.preexisting_x11:
            # Forwarded X11, don't take over their whole screen
            i.hide()
            self.window.set_resizable(True)
        else:
            # Normal install, full screen
            self.window.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_DESKTOP)
            if gtk.gdk.screen_height() < 600:
                i.hide()
            self.window.maximize()

        if flags.debug:
            self.mainxml.get_widget("debugButton").show_now()
        self.installFrame = self.mainxml.get_widget("installFrame")

    def connectSignals(self):
        sigs = { "on_nextButton_clicked": self.nextClicked,
            "on_rebootButton_clicked": self.nextClicked,
            "on_closeButton_clicked": self._doExit,                 
            "on_backButton_clicked": self.prevClicked,
            "on_debugButton_clicked": self.debugClicked,
            "on_mainWindow_key_release_event": self.keyRelease,
            "on_mainWindow_delete_event": self._doExitConfirm, }
        self.mainxml.signal_autoconnect(sigs)

    def loadGlade(self):
        self.mainxml = gtk.glade.XML(findGladeFile("anaconda.glade"),
                                     domain="anaconda")

    def setup_theme(self):
        settings = gtk.settings_get_default()
        settings.set_long_property("gtk-button-images", 0, "gui.py:setup_theme")

    def setup_window (self, window_reload):
        self.setLtR()

        if window_reload:
            self.window.destroy()
        elif flags.livecdInstall:
            pixbuf = getPixbuf("anaconda.png")
            gtk.window_set_default_icon(pixbuf)

        self.loadGlade()
        self.window = self.mainxml.get_widget("mainWindow")

        self.createWidgets()
        self.connectSignals()
        self.setScreen()
        self.window.show()
        # calling present() will focus the window in the winodw manager so
        # the mnemonics work without additional clicking
        self.window.present()
            
    def busyCursorPush(self):
        rootPushBusyCursor()
        
    def busyCursorPop(self):
        rootPopBusyCursor()
        
    def run (self):
        self.setup_theme()
        self.setup_window(False)
        gtk.main()
            
class InstallControlState:
    def __init__ (self, cw):
        self.cw = cw
        self.prevEnabled = True
        self.nextEnabled = True
        self.title = _("Install Window")
        self.grabNext = True

    def setTitle (self, title):
        self.title = title
        self.cw.update (self)
        
    def getTitle (self):
        return self.title

    def setPrevEnabled (self, value):
        if value == self.prevEnabled: return
        self.prevEnabled = value
        self.cw.update (self)

    def getPrevEnabled (self):
        return self.prevEnabled
    
    def setNextEnabled (self, value):
        if value == self.nextEnabled: return
        self.nextEnabled = value
        self.cw.update (self)

    def getNextEnabled (self):
        return self.nextEnabled

    def setScreenPrev (self):
        self.cw.prevClicked ()

    def setScreenNext (self):
        self.cw.nextClicked ()

    def setGrabNext (self, value):
        self.grabNext = value
        self.cw.update (self)

    def getGrabNext (self):
        return self.grabNext

    def getICW (self):
        return self.cw
