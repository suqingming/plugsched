#!/usr/bin/env python2

import sys
from yaml import load, dump
try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    print >> sys.stderr, "WARNING: YAML CLoader is not presented, it can be slow."
    from yaml import Loader, Dumper
import sh
from sh import rsync, cp, glob as _glob
from sched_boundary import check_sym_duplicy
from multiprocessing import cpu_count
from tempfile import mkdtemp
import coloredlogs
import logging
import uuid
import stat
import fire
import os

def glob(pattern, _cwd='.'):
    return _glob(os.path.join(_cwd, pattern))

class ShutdownHandler(logging.StreamHandler):
    def emit(self, record):
        if record.levelno >= logging.CRITICAL:
            raise Exception("Fatal")

coloredlogs.install(level='INFO')
logging.getLogger().addHandler(ShutdownHandler())

class Plugsched(object):
    def __init__(self, mod_path, vmlinux):
        self.plugsched_path = os.path.dirname(os.path.realpath(__file__))
        self.mod_path = os.path.abspath(mod_path)
        self.vmlinux = os.path.abspath(vmlinux)
        self.search_springboard = sh.Command(os.path.join(self.plugsched_path, 'tools/springboard_search.sh'))

        plugsched_sh = sh(_cwd=self.plugsched_path)
        mod_sh = sh(_cwd=self.mod_path)

        self.plugsched_sh, self.mod_sh = plugsched_sh, mod_sh

        with open(os.path.join(self.plugsched_path, 'sched_boundary/sched_boundary.yaml')) as f:
            self.config = load(f, Loader)
        self.file_mapping = {
            'sched_boundary/sched_boundary.py': './',
            'sched_boundary/process.py': './',
            'sched_boundary/sched_boundary.yaml': './',
            'sched_boundary/fake.c': './',
            'tools/symbol_resolve': './',
            'src/*.[ch]': 'kernel/sched/mod',
            'src/.gitignore': './',
            'src/Makefile': 'kernel/sched/mod/',
            'src/scheduler.lds': 'kernel/sched/mod/',
            'src/Makefile.plugsched': './'
        }
        self.threads = cpu_count()
        self.mod_files = self.config['mod_files']
        self.mod_srcs = [f for f in self.mod_files if f.endswith('.c')]
        self.mod_hdrs = [f for f in self.mod_files if f.endswith('.h')]
        self.mod_objs = [f[:-2]+'.o' for f in self.mod_srcs]
        self.extracted_mod_srcs = [os.path.join('kernel/sched/mod', os.path.basename(f)) for f in self.mod_srcs]
        self.extracted_mod_files = self.extracted_mod_srcs + self.mod_hdrs

    def apply_patch(self, f, **kwargs):
        self.mod_sh.patch(input=os.path.join(self.plugsched_path, 'src', f), strip=1, **kwargs)

    def make(self, objs=[], **kwargs):
        self.mod_sh.make('sched_mod',
                         'AR="echo"',
                         objs,
                         *['%s=%s' % i for i in kwargs.items()],
                         file='Makefile.plugsched',
                         jobs=self.threads)

    def fix_up(self):
        self.mod_sh.sed("s/#include \"/#include \"..\//g;"  + \
                        "/EXPORT_SYMBOL/d;"                 + \
                        "/initcall/d;"                      + \
                        "/early_param/d;"                   + \
                        "/\<__init\>/d;"                    + \
                        "/\<__initdata\>/d;"                + \
                        "/__setup/d;"                       + \
                        "s/struct atomic_t /atomic_t /g",
                        self.extracted_mod_srcs,
                        in_place=True)

    def extract(self):
        logging.info('Extracting scheduler module objs: %s', ' '.join(self.mod_objs))
        self.make(SCHED_MOD_STAGE = 'collect')
        self.make(SCHED_MOD_STAGE = 'analyze',
                  SYSTEM_MAP      = './System.map')
        self.make(SCHED_MOD_STAGE = 'extract',
                  objs            = self.mod_objs)
        with open(os.path.join(self.mod_path, 'kernel/sched/mod/export_jump.h'), 'w') as f:
            sh.sort(glob('kernel/sched/*.export_jump.h', _cwd=self.mod_path), _out=f)

    def create_mod(self, kernel_src):
        logging.info('Creating mod build directory structure')
        rsync(kernel_src + '/', self.mod_path + '/', archive=True, verbose=True, delete=True, exclude='.git', filter=':- .gitignore')
        self.mod_sh.mkdir('kernel/sched/mod', parents=True)

        for f, t in self.file_mapping.items():
            self.mod_sh.cp(glob(f, _cwd=self.plugsched_path), t, recursive=True)


    def cmd_init(self, kernel_src, system_map, sym_vers, kernel_config, makefile):
        self.create_mod(kernel_src)
        self.plugsched_sh.cp(system_map,    self.mod_path, force=True)
        self.plugsched_sh.cp(sym_vers,      self.mod_path, force=True)
        self.plugsched_sh.cp(kernel_config, self.mod_path + '/.config', force=True)
        self.plugsched_sh.cp(makefile,      self.mod_path, force=True)
        self.plugsched_sh.cp(self.vmlinux,  self.mod_path, force=True)

        logging.info('Patching kernel kbuild system')
        self.apply_patch('kbuild.patch')

        # precompile some files to avoid ugly building trouble
        self.mod_sh.make(
            'scripts/mod/',
            'arch/x86/platform/',
            'arch/x86/purgatory/',
            'arch/x86/realmode/rm/',
            'arch/x86/entry/vdso/',
            'arch/x86/lib/',
            'arch/x86/oprofile/',
            jobs=self.threads
        )

        self.extract()
        logging.info('Fixing up extracted scheduler module')
        self.fix_up()
        logging.info('Patching extracted scheduler module')
        self.apply_patch('module.patch')

        # special handle for builtin springboard kernel version
        try:
            sh.grep('label_recover', os.path.join(self.mod_path, 'kernel/sched/core.c'))
        except:
            logging.info('Patching dynamic springboard')
            self.apply_patch('dynamic_springboard.patch')

        try:
            springboard = list(self.search_springboard(self.vmlinux))

            if len(springboard) != 2:
                logging.error("Search springboard faild!")
                exit(-1)

            """
            springboard[0] is the value of sched_springboard var.
            springboard[1] is the stack size of __schedule in vmlinux.
            """
            with open(os.path.join(self.mod_path, 'kernel/sched/mod/Makefile'), 'a') as f:
                f.write('ccflags-y += -DSPRINGBOARD=' + str(springboard[0]))
                f.write('ccflags-y += -DSTACKSIZE_SCHEDULE=' + str(springboard[1]))

        except sh.ErrorReturnCode:
            logging.error("Search springboard faild!")
            exit(-1)

        logging.info("Succeed!")

    def cmd_build(self):
        if not os.path.exists(self.mod_path):
            logging.fatal("plugsched: Can't find %s", self.mod_path)
        logging.info("Preparing rpmbuild environment")
        rpmbuild_root = os.path.join(self.plugsched_path, 'rpmbuild')
        self.plugsched_sh.rm('rpmbuild', recursive=True, force=True)
        self.plugsched_sh.mkdir('rpmbuild')
        rpmbase_sh = sh(_cwd=rpmbuild_root)
        rpmbase_sh.mkdir(['BUILD','RPMS','SOURCES','SPECS','SRPMS'])
        VERSION = self.mod_sh.awk('-F=', '/^VERSION/{print $2}', 'Makefile').strip()
        PATCHLEVEL = self.mod_sh.awk('-F=', '/^PATCHLEVEL/{print $2}', 'Makefile').strip()
        SUBLEVEL = self.mod_sh.awk('-F=', '/^SUBLEVEL/{print $2}', 'Makefile').strip()
        KVER = '%s.%s.%s' % (VERSION, PATCHLEVEL, SUBLEVEL)

        KREL = self.mod_sh.awk('-F=', '/^EXTRAVERSION/{print $2}', 'Makefile').strip(' \n-')
        if len(KREL) == 0:
            logging.fatal('''Maybe you are using plugsched on non-released kernel,
                          please set EXTRAVERSION in Makefile (%s) before build kernel''',
                          os.path.join(self.mod_path, 'Makefile'))

        # strip ARCH
        for arch in ['.x86_64', '.aarch64']:
            idx = KREL.find(arch)
            if idx != -1:
                KREL = KREL[:idx]

        self.plugsched_sh.cp('module-contrib/scheduler.spec', os.path.join(rpmbuild_root, 'SPECS'), force=True)
        rpmbase_sh.rpmbuild('--define', '%%_outdir %s' % os.path.realpath(self.plugsched_path + '/module-contrib'),
                            '--define', '%%_topdir %s' % os.path.realpath(rpmbuild_root),
                            '--define', '%%_dependdir %s' % os.path.realpath(self.plugsched_path),
                            '--define', '%%_kerneldir %s' % os.path.realpath(self.mod_path),
                            '--define', '%%KVER %s' % KVER,
                            '--define', '%%KREL %s' % KREL,
                            '--define', '%%threads %d' % self.threads,
                            '-bb', 'SPECS/scheduler.spec')
        logging.info("Succeed!")

class PlugschedCLI(object):
    """ A command line interface for plugsched """

    def dep(self, j=1):
        """ Building dependencies (gcc-python-plugin)

        :param j: Number of threads. "-j N" is okay while "-jN" is not allowed.
        """
        root_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)))
        plugsched_sh = sh(_cwd = root_dir)
        plugsched_sh.git.submodule.update(init = True)
        depsh = sh(_cwd = os.path.join(root_dir, 'gcc-python-plugin'))
        with sh.contrib.sudo:
            sh.yum.install('python-devel', 'gcc', 'gcc-plugin-devel', _fg=True)
        depsh.make(jobs=j)

    def extract_src(self, kernel_src_rpm, target_dir):
        """ extract kernel source code from kernel-src rpm

        :param kernel_src_rpm: path of kernel source rpm
        :param target_dir: directory to place kernel source code
        """

        rpmbuild_root = mkdtemp()
        sh.rpmbuild('--define', '%%_topdir %s' % rpmbuild_root,
                    '-rp', kernel_src_rpm)

        src = glob('kernel*/linux*', rpmbuild_root + '/BUILD/')

        if len(src) != 1:
            logging.fatal("find multi kernel source, fuzz ...")

        rsync(src[0] + '/', target_dir + '/', archive=True, verbose=True, delete=True)

        # certificates for CONFIG_MODULE_SIG_KEY & CONFIG_SYSTEM_TRUSTED_KEYS
        for pem in glob('*.pem', rpmbuild_root + '/SOURCES/'):
            sh.cp(pem, target_dir + '/certs', force=True)

        sh.rm(rpmbuild_root, recursive=True, force=True)

    def init(self, release_kernel, kernel_src, mod_path):
        """ Initialize a scheduler module for a specific kernel release and product

        :param kernel_release: `uname -r` of target kernel to be hotpluged
        :param kernel_src: kernel source code directory
        :param mod_path: target working directory to develop new scheduler module
        """

        vmlinux = '/usr/lib/debug/lib/modules/' + release_kernel + '/vmlinux'
        if not os.path.exists(vmlinux):
            logging.fatal("%s not found, please install kernel-debuginfo-%s.rpm", vmlinux, release_kernel)

        system_map    = '/usr/src/kernels/' + release_kernel + '/System.map'
        sym_vers      = '/usr/src/kernels/' + release_kernel + '/Module.symvers'
        kernel_config = '/usr/src/kernels/' + release_kernel + '/.config'
        makefile      = '/usr/src/kernels/' + release_kernel + '/Makefile'

        if not os.path.exists(kernel_config):
            logging.fatal("%s not found, please install kernel-devel-%s.rpm", kernel_config, release_kernel)

        self.plugsched = Plugsched(mod_path, vmlinux)
        self.plugsched.cmd_init(kernel_src, system_map, sym_vers, kernel_config, makefile)

    def dev_init(self, kernel_src, mod_path):
        """ Initialize plugsched development envrionment from kernel source code

        :param kernel_src: kernel source code directory
        :param mod_path: target working directory to develop new scheduler module
        """

        if not os.path.exists(kernel_src):
            logging.fatal("Kernel source directory not exists")

        vmlinux = os.path.join(kernel_src, 'vmlinux')
        if not os.path.exists(vmlinux):
            logging.fatal("%s not found, please execute `make -j %s` firstly", vmlinux, cpu_count())

        system_map    = os.path.join(kernel_src, 'System.map')
        sym_vers      = os.path.join(kernel_src, 'Module.symvers')
        kernel_config = os.path.join(kernel_src, '.config')
        makefile      = os.path.join(kernel_src, 'Makefile')

        if not os.path.exists(kernel_config):
            logging.fatal("kernel config %s not found", kernel_config)

        self.plugsched = Plugsched(mod_path, vmlinux)
        self.plugsched.cmd_init(kernel_src, system_map, sym_vers, kernel_config, makefile)

    def build(self, mod_path):
        """ Build a scheduler module rpm package for a specific kernel release and product

        :param mod_path: target working directory to develop new scheduler module
        """

        vmlinux = os.path.join(mod_path, 'vmlinux')
        self.plugsched = Plugsched(mod_path, vmlinux)
        self.plugsched.cmd_build()

    def self_debug(self, func, *args, **kwargs):
        """ Debug plugsched tool itself

        :param func: The process of plugsched to be debugged
        :param args: Any arguments to be passed to func
        :param kwargs: Any positional arguments to be passed to func
        """
        self.plugsched = Plugsched(*args, **kwargs)
        getattr(self.plugsched, func)(*args, **kwargs)

if __name__ == '__main__':
    fire.Fire(PlugschedCLI)
