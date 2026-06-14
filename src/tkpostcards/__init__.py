# -*- encoding: utf-8 -*-
import os
from gettext import gettext as _
import configparser

import click

def config(confile=None):
    if confile is None:
        confile = 'postcards.conf'
    config = configparser.ConfigParser()
    config.read(confile)
    return config

class Common(object):
    def __init__(self, conffile=None, datadir=None, importdir=None, tmpdir=None, debug=None):
        self.conffile = conffile
        self.conf = config(self.conffile)

        if datadir is None:
            datadir = config().get('DEFAULT', 'datadir', fallback=None)
        self.datadir = os.path.abspath(datadir or 'data')

        if importdir is None:
            importdir = config().get('DEFAULT', 'importdir', fallback=None)
        self.importdir = os.path.abspath(importdir or 'import')

        if tmpdir is None:
            tmpdir = config().get('DEFAULT', 'tmpdir', fallback=None)
        self.tmpdir = os.path.abspath(tmpdir or 'tmp')

        self.file_format = config().get('DEFAULT', 'file_format', fallback='tiff')

        self.debug = debug

@click.group()
@click.option('--conffile', default='postcards.conf', help=_("Configuration file"))
@click.option('--datadir', default=None, help=_("Image and JSON storage directory"))
@click.option('--importdir', default=None, help=_("Scanned image import directory"))
@click.option('--tmpdir', default=None, help=_("Temporary directory"))
@click.option('--debug/--no-debug', default=False, help=_("Enable/disable debug"))
@click.pass_context
def cli(ctx, conffile, datadir, importdir, tmpdir, debug):
    """Command group."""
    ctx.obj = Common(conffile, datadir, importdir, tmpdir, debug)

