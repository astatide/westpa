import sys, os, re, logging
from optparse import OptionParser
from command_optparse import CommandOptionParser
import wemd
import wemd.rc

class WECmdLineTool(object):
    RC_SHORT_OPTION = '-r'
    RC_LONG_OPTION  = '--rcfile'

    @classmethod
    def add_rc_option(cls, parser):
        parser.add_option(cls.RC_SHORT_OPTION, cls.RC_LONG_OPTION,
                          dest='rcfile', 
                          help='runtime configuration is in RCFILE '
                              +'(default: %s)' % wemd.rc.RC_DEFAULT_FILENAME,
                          default=wemd.rc.RC_DEFAULT_FILENAME)
        
    usage = '%prog [options] COMMAND [...]'
    description = None
    
    def __init__(self):    
        self.runtime_config = None
        self.sim_config = {}
        self.sim_config_src = None
        self.log = logging.getLogger('%s.%s' 
                                     % (__name__, self.__class__.__name__))
        
    def read_runtime_config(self, opts_or_filename):
        try:
            rcfile = opts_or_filename.rcfile
        except AttributeError:
            rcfile = opts_or_filename
            
        try:
            self.runtime_config = wemd.rc.read_config(rcfile)
        except IOError, e:
            sys.stderr.write('cannot open runtime config file: %s\n' % e)
            sys.exit(wemd.rc.EX_RTC_ERROR)    
        wemd.rc.configure_logging(self.runtime_config)
        
    def load_sim_manager(self, load_sim_config = True):
        driver_name = self.runtime_config.get('sim_manager.driver', 'serial').lower()
        Manager = wemd.sim_managers.get_sim_manager(driver_name)   
        self.sim_manager = Manager()
        self.sim_manager.runtime_init(self.runtime_config, load_sim_config)
        return self.sim_manager
        
    def run(self):
        raise NotImplementedError

class WECmdLineMultiTool(WECmdLineTool):
    
    def __init__(self):
        super(WECmdLineMultiTool,self).__init__()
        self.command_parser = CommandOptionParser(usage = self.usage,
                                                  description = self.description)
        
        self.global_opts = None
        self.command = None
        self.command_args = None

    def make_parser(self, usage_tail = '[options]', description = None):
        cmdname = self.command.name
        description = description or self.command.description or None
        parser = OptionParser(usage='%%prog %s %s' % (self.command.name,
                                                      usage_tail),
                              description = description)
        return parser

    def cmd_help(self, args):
        try:
            command = self.command_parser.get_command(args[0])
        except (KeyError,IndexError,TypeError):
            self.command_parser.print_help()
        else:
            if command.accepts_help_arg:
                self.command = command
                command(['--help'])
            else:
                self.command_parser.print_help()
        sys.exit(0)
        
    def run(self):
        self.command_parser.add_command('help', 'show help', self.cmd_help, 
                                        False)
        (self.global_opts, self.command_args) = self.command_parser.parse_args()
        self.read_runtime_config(self.global_opts)
        self.command = self.command_parser.command
        self.command(self.command_args)
