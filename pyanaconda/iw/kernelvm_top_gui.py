#
# kernelvm_top_gui.py: top menu of the installer.
#
# Copyright (C) 2011, Masami Ichikawa <masami@fedoraproject.org>.  All rights reserved.
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

import gtk
from pyanaconda import gui
import sys
from iw_gui import *

from pyanaconda.constants import *
import gettext
_ = lambda x: gettext.ldgettext("anaconda", x)

class KernelvmTopWindow (InstallWindow):

    windowTitle = "Kernel/VM" #N_("KernelVM")

    def __init__ (self, ics):
        InstallWindow.__init__ (self, ics)
        ics.setGrabNext (1)
        self.anaconda = None

    def getScreen (self, anaconda):
        self.anaconda = anaconda

        box = gtk.VBox (False, 10)

        pix = gui.readImageFromFile ("kernelvm.png")
        if pix:
            a = gtk.Alignment ()
            a.add (pix)
            a.set (0.5, 0.5, 1.0, 1.0)
	    a.set_size_request(200, -1)
            box.pack_start (a, False)

        box.pack_start(self.createHbox())

        return box

    def createHbox(self):
        labels = []
        labels.append(gtk.Label("Ore ore kernel installer for Kernel/VM LT"))
        labels.append(gtk.Label("twitter/hatena: masami256"))
        labels.append(gtk.Label("masami@fedoraproject.org"))

        vbox = gtk.VBox()
        vbox.pack_start(labels[0], False)

        for i in range(len(labels) - 1):
            hbox = gtk.HBox(False, 10)
            hbox.pack_end(labels[i + 1], False)
            vbox.pack_start(hbox, False)
        
        return vbox

    def getNext (self):
        pass
