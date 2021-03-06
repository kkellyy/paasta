import argparse
import json
import os
import socket
import sys

from botocore.session import Session

from paasta_tools.cli.cmds.check import makefile_responds_to
from paasta_tools.cli.cmds.cook_image import paasta_cook_image
from paasta_tools.cli.utils import get_instance_config
from paasta_tools.cli.utils import lazy_choices_completer
from paasta_tools.cli.utils import list_instances
from paasta_tools.cli.utils import list_services
from paasta_tools.cli.utils import pick_random_port
from paasta_tools.mesos_tools import find_mesos_leader
from paasta_tools.mesos_tools import MESOS_MASTER_PORT
from paasta_tools.utils import _run
from paasta_tools.utils import DEFAULT_SOA_DIR
from paasta_tools.utils import get_username
from paasta_tools.utils import load_system_paasta_config
from paasta_tools.utils import NoConfigurationForServiceError
from paasta_tools.utils import NoDeploymentsAvailable
from paasta_tools.utils import NoDockerImageError
from paasta_tools.utils import paasta_print
from paasta_tools.utils import PaastaColors
from paasta_tools.utils import PaastaNotConfiguredError
from paasta_tools.utils import SystemPaastaConfig

DEFAULT_SPARK_WORK_DIR = '/spark_driver'
DEFAULT_SPARK_DOCKER_IMAGE_PREFIX = 'paasta-spark-run'
DEFAULT_SPARK_DOCKER_REGISTRY = 'docker-dev.yelpcorp.com'
DEFAULT_SPARK_MESOS_SECRET_FILE = '/nail/etc/paasta_spark_secret'


def add_subparser(subparsers):
    list_parser = subparsers.add_parser(
        'spark-run',
        help="Run Spark on the PaaSTA cluster",
        description=(
            "'paasta spark-run' launches a Spark cluster on PaaSTA. "
            "It analyzes soa-configs and command line arguments to invoke "
            "a 'docker run'. By default, it will pull the Spark service "
            "image from the registry unless the --build option is used.\n\n"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    group = list_parser.add_mutually_exclusive_group()
    group.add_argument(
        '-b', '--build',
        help="Build the docker image from scratch using the local Makefile's cook-image target.",
        action='store_true',
        default=False,
    )
    group.add_argument(
        '-I', '--image',
        help="Use the provided image to start the Spark driver and executors.",
    )

    list_parser.add_argument(
        '-s', '--service',
        help="The name of the service from which the Spark image is built.",
        default='spark',
    ).completer = lazy_choices_completer(list_services)

    list_parser.add_argument(
        '-i', '--instance',
        help=("Start a docker run for a particular instance of the service."),
        default='client',
    ).completer = lazy_choices_completer(list_instances)

    # Restrict usage to norcal-devc and pnw-devc for now.
    list_parser.add_argument(
        '-c', '--cluster',
        help=(
            "The name of the cluster you wish to run Spark on."
        ),
        choices=['norcal-devc', 'pnw-devc', 'mesosstage'],
        required=True,
    )

    list_parser.add_argument(
        '-j', '--jars',
        help="Comma-separated list of local jars to include on the driver and executor classpaths.",
    )

    list_parser.add_argument(
        '-p', '--pool',
        help="Name of the resource pool to run the Spark job.",
        default='default',
    )

    list_parser.add_argument(
        '-w', '--work-dir',
        default='%s:%s' % (os.getcwd(), DEFAULT_SPARK_WORK_DIR),
        help="The read-write volume to mount in format local_abs_dir:container_abs_dir",
    )

    list_parser.add_argument(
        '-y', '--yelpsoa-config-root',
        dest='yelpsoa_config_root',
        help='A directory from which yelpsoa-configs should be read from.',
        default=DEFAULT_SOA_DIR,
    )

    list_parser.add_argument(
        '-C', '--cmd',
        help="Run Spark with the spark-shell, pyspark, spark-submit or jupyter command.",
        default='pyspark',
    )

    list_parser.add_argument(
        '-d', '--dry-run',
        help='Shows the arguments supplied to docker as json.',
        action='store_true',
        default=False,
    )

    list_parser.add_argument(
        '--mesos-principal',
        help="Mesos principal (username) to run a framework on Mesos.",
        default='spark',
    )

    list_parser.add_argument(
        '--mesos-secret',
        help="Mesos secret (password) to run a framework on Mesos.",
    )

    list_parser.add_argument(
        '--executor-memory',
        type=int,
        help='Size of Spark executor memory in GB',
        default=4,
    )

    list_parser.add_argument(
        '--executor-cores',
        type=int,
        help='Number of CPU cores for each Spark executor',
        default=2,
    )

    list_parser.add_argument(
        '--max-cores',
        type=int,
        help='The total number of CPU cores for all Spark executors',
        default=4,
    )

    list_parser.add_argument(
        '--driver-max-result-size',
        type=int,
        help='Limit of total size in GB of serialized results of all partitions',
    )

    list_parser.add_argument(
        '--driver-memory',
        type=int,
        help='Size of Spark driver memory in GB',
    )

    list_parser.add_argument(
        '--driver-cores',
        type=int,
        help='Number of CPU cores for the Spark driver',
    )

    list_parser.set_defaults(command=paasta_spark_run)


def get_docker_run_cmd(
    container_name,
    volumes,
    env,
    docker_img,
    docker_cmd,
):
    cmd = ['paasta_docker_wrapper', 'run']
    cmd.append('--rm')
    cmd.append('--net=host')
    cmd.append('--interactive=true')
    cmd.append('--tty=true')

    cmd.append('--user=%d:%d' % (os.geteuid(), os.getegid()))
    cmd.append('--name=%s' % container_name)
    for k, v in env.items():
        cmd.append('--env')
        cmd.append('%s=%s' % (k, v))
    for volume in volumes:
        cmd.append('--volume=%s' % volume)
    cmd.append('%s' % docker_img)
    cmd.extend(('sh', '-c', docker_cmd))

    return cmd


def get_spark_env(
    args,
    spark_conf,
):
    spark_env = {}

    creds = Session().get_credentials()
    spark_env['AWS_ACCESS_KEY_ID'] = creds.access_key
    spark_env['AWS_SECRET_ACCESS_KEY'] = creds.secret_key

    # Run spark (and mesos framework) as root.
    spark_env['SPARK_USER'] = 'root'
    spark_env['SPARK_OPTS'] = spark_conf

    # Default configs to start the jupyter notebook server
    if args.cmd == 'jupyter':
        dirs = args.work_dir.split(':')
        spark_env['JUPYTER_RUNTIME_DIR'] = dirs[1] + '/.jupyter'
        spark_env['JUPYTER_DATA_DIR'] = dirs[1] + '/.jupyter'

    return spark_env


def get_spark_conf_str(
    args,
    container_name,
    spark_ui_port,
    docker_img,
    system_paasta_config,
    volumes,
):
    spark_conf = list()
    spark_conf.append('--conf spark.app.name=%s' % container_name)
    spark_conf.append('--conf spark.ui.port=%d' % spark_ui_port)

    cluster_fqdn = system_paasta_config.get_cluster_fqdn_format().format(cluster=args.cluster)
    mesos_address = '{}:{}'.format(
        find_mesos_leader(cluster_fqdn),
        MESOS_MASTER_PORT,
    )
    spark_conf.append('--conf spark.master=mesos://%s' % mesos_address)

    spark_conf.append('--conf spark.cores.max=%d' % args.max_cores)
    spark_conf.append('--conf spark.executor.memory=%dg' % args.executor_memory)
    spark_conf.append('--conf spark.executor.cores=%d' % args.executor_cores)

    if args.driver_max_result_size:
        spark_conf.append('--conf spark.driver.maxResultSize=%dg' % args.driver_max_result_size)
    if args.driver_memory:
        spark_conf.append('--conf spark.driver.memory=%dg' % args.driver_memory)
    if args.driver_cores:
        spark_conf.append('--conf spark.driver.cores=%d' % args.driver_cores)

    spark_conf.append('--conf spark.mesos.executor.docker.image=%s' % docker_img)
    if not args.build and not args.image:
        spark_conf.append('--conf spark.mesos.uris=file:///root/.dockercfg')

    if args.jars:
        spark_conf.append('--conf spark.jars=%s' % args.jars)

    spark_conf.append('--conf spark.mesos.principal=%s' % args.mesos_principal)
    if not args.mesos_secret:
        try:
            with open(DEFAULT_SPARK_MESOS_SECRET_FILE, 'r') as f:
                mesos_secret = f.read()
                spark_conf.append('--conf spark.mesos.secret=%s' % mesos_secret)
        except IOError:
            paasta_print(
                'Cannot load mesos secret from %s' % DEFAULT_SPARK_MESOS_SECRET_FILE,
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        spark_conf.append('--conf spark.mesos.secret=%s' % args.mesos_secret)

    # derby.system.home property defaulting to '.',
    # which requires directory permission changes.
    spark_conf.append('--conf spark.driver.extraJavaOptions=-Dderby.system.home=/tmp/derby')

    spark_conf.append('--conf spark.mesos.constraints=pool:%s' % args.pool)

    spark_conf.append('--conf spark.mesos.executor.docker.volumes=%s' % ','.join(volumes))

    return ' '.join(spark_conf)


def run_docker_container(
    container_name,
    volumes,
    environment,
    docker_img,
    docker_cmd,
    dry_run,
):
    docker_run_args = dict(
        container_name=container_name,
        volumes=volumes,
        env=environment,
        docker_img=docker_img,
        docker_cmd=docker_cmd,
    )
    docker_run_cmd = get_docker_run_cmd(**docker_run_args)
    joined_docker_run_cmd = ' '.join(docker_run_cmd)

    if dry_run:
        paasta_print(json.dumps(docker_run_cmd))
        return 0
    else:
        paasta_print('Running docker command:\n%s' % PaastaColors.grey(joined_docker_run_cmd))

    os.execlp('paasta_docker_wrapper', *docker_run_cmd)
    return 0


def configure_and_run_docker_container(
        args,
        docker_img,
        instance_config,
        system_paasta_config,
):
    volumes = list()
    for volume in instance_config.get_volumes(system_paasta_config.get_volumes()):
        if os.path.exists(volume['hostPath']):
            volumes.append('%s:%s:%s' % (volume['hostPath'], volume['containerPath'], volume['mode'].lower()))
        else:
            paasta_print(
                PaastaColors.yellow(
                    "Warning: Path %s does not exist on this host. Skipping this binding." % volume['hostPath'],
                ),
            )

    spark_ui_port = pick_random_port(args.service)
    container_name = 'paasta_spark_run_%s_%s' % (get_username(), spark_ui_port)

    spark_conf_str = get_spark_conf_str(
        args=args,
        container_name=container_name,
        spark_ui_port=spark_ui_port,
        docker_img=docker_img,
        system_paasta_config=system_paasta_config,
        volumes=volumes,
    )

    # Spark client specific volumes
    volumes.append('%s:rw' % args.work_dir)
    volumes.append('/etc/passwd:/etc/passwd:ro')
    volumes.append('/etc/group:/etc/group:ro')

    if args.cmd is None:
        docker_cmd = instance_config.get_cmd()
    else:
        docker_cmd = args.cmd

    if docker_cmd is None:
        paasta_print("A command is required, pyspark, spark-shell, spark-submit or jupyter", file=sys.stderr)
        return 1

    # Default cli options to start the jupyter notebook server.
    if docker_cmd == 'jupyter':
        docker_cmd = 'jupyter notebook -y --ip=%s --notebook-dir=%s' % (
            socket.getfqdn(), args.work_dir.split(':')[1],
        )
    # Spark options are passed as options to pyspark and spark-shell.
    # For jupyter, environment variable SPARK_OPTS is set instead.
    elif docker_cmd in ['pyspark', 'spark-shell']:
        docker_cmd = docker_cmd + ' ' + spark_conf_str
    elif docker_cmd.startswith('spark-submit'):
        docker_cmd = 'spark-submit ' + spark_conf_str + docker_cmd[len('spark-submit'):]

    environment = instance_config.get_env_dictionary()
    environment.update(
        get_spark_env(
            args,
            spark_conf_str,
        ),
    )

    paasta_print('\nSpark Monitoring URL http://%s:%d\n' % (socket.getfqdn(), spark_ui_port))

    return run_docker_container(
        container_name=container_name,
        volumes=volumes,
        environment=environment,
        docker_img=docker_img,
        docker_cmd=docker_cmd,
        dry_run=args.dry_run,
    )


def build_and_push_docker_image(args):
    """
    Build an image if the default Spark service image is not preferred.
    The image needs to be pushed to a registry for the Spark executors
    to pull.
    """
    if not makefile_responds_to('cook-image'):
        paasta_print(
            "A local Makefile with a 'cook-image' target is required for --build",
            file=sys.stderr,
        )
        return None

    default_tag = '%s-%s' % (DEFAULT_SPARK_DOCKER_IMAGE_PREFIX, get_username())
    docker_tag = os.environ.get('DOCKER_TAG', default_tag)
    os.environ['DOCKER_TAG'] = docker_tag

    cook_return = paasta_cook_image(
        args=None,
        service=args.service,
        soa_dir=args.yelpsoa_config_root,
    )
    if cook_return is not 0:
        return None

    docker_url = '%s/%s' % (DEFAULT_SPARK_DOCKER_REGISTRY, docker_tag)
    command = 'docker tag %s %s' % (docker_tag, docker_url)
    paasta_print(PaastaColors.grey(command))
    retcode, _ = _run(command, stream=True)
    if retcode is not 0:
        return None

    command = 'docker push %s' % docker_url
    paasta_print(PaastaColors.grey(command))
    retcode, output = _run(command, stream=True)
    if retcode is not 0:
        return None

    return docker_url


def validate_work_dir(s):
    dirs = s.split(':')
    if len(dirs) != 2:
        paasta_print(
            "work-dir %s is not in format local_abs_dir:container_abs_dir" % s,
            file=sys.stderr,
        )
        sys.exit(1)

    for d in dirs:
        if not os.path.isabs(d):
            paasta_print("%s is not an absolute path" % d, file=sys.stderr)
            sys.exit(1)


def paasta_spark_run(args):
    # argparse does not work as expected with both default and
    # type=validate_work_dir.
    validate_work_dir(args.work_dir)

    try:
        system_paasta_config = load_system_paasta_config()
    except PaastaNotConfiguredError:
        paasta_print(
            PaastaColors.yellow(
                "Warning: Couldn't load config files from '/etc/paasta'. This indicates"
                "PaaSTA is not configured locally on this host, and local-run may not behave"
                "the same way it would behave on a server configured for PaaSTA.",
            ),
            sep='\n',
        )
        system_paasta_config = SystemPaastaConfig({"volumes": []}, '/etc/paasta')

    # Use the default spark:client instance configs if not provided
    try:
        instance_config = get_instance_config(
            service=args.service,
            instance=args.instance,
            cluster=args.cluster,
            load_deployments=args.build is False and args.image is None,
            soa_dir=args.yelpsoa_config_root,
        )
    except NoConfigurationForServiceError as e:
        paasta_print(str(e), file=sys.stderr)
        return 1
    except NoDeploymentsAvailable:
        paasta_print(
            PaastaColors.red(
                "Error: No deployments.json found in %(soa_dir)s/%(service)s."
                "You can generate this by running:"
                "generate_deployments_for_service -d %(soa_dir)s -s %(service)s" % {
                    'soa_dir': args.yelpsoa_config_root,
                    'service': args.service,
                },
            ),
            sep='\n',
            file=sys.stderr,
        )
        return 1

    if args.build:
        docker_url = build_and_push_docker_image(args)
        if docker_url is None:
            return 1
    elif args.image:
        docker_url = args.image
    else:
        try:
            docker_url = instance_config.get_docker_url()
        except NoDockerImageError:
            paasta_print(
                PaastaColors.red(
                    "Error: No sha has been marked for deployment for the %s deploy group.\n"
                    "Please ensure this service has either run through a jenkins pipeline "
                    "or paasta mark-for-deployment has been run for %s\n" % (
                        instance_config.get_deploy_group(), args.service,
                    ),
                ),
                sep='',
                file=sys.stderr,
            )
            return 1
        paasta_print(
            "Please wait while the image (%s) is pulled (times out after 5m)..." % docker_url,
            file=sys.stderr,
        )
        retcode, _ = _run('sudo -H docker pull %s' % docker_url, stream=True, timeout=300)
        if retcode != 0:
            paasta_print(
                "\nPull failed. Are you authorized to run docker commands?",
                file=sys.stderr,
            )
            return 1

    return configure_and_run_docker_container(
        args,
        docker_img=docker_url,
        instance_config=instance_config,
        system_paasta_config=system_paasta_config,
    )
