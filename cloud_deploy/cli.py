#!/usr/bin/env python
"""
Script for deploying services (provided by Docker containers) in the cloud.

Author: Andrew Davison, CNRS, 2016-2017

Usage: deploy.py [OPTIONS] COMMAND [ARGS]...

Options:
  --debug
  --help   Show this message and exit.

Commands:
  bootstrap  Set-up the development and build environment.
  build      Build a Docker image locally and push to...
  database   Sub-command for managing database services.
  launch     Launch a new service.
  log        Display the log for a given service.
  node       Sub-command for managing server nodes.
  redeploy   Redeploy a running service.
  services   Display a list of services.
  terminate  Terminate a given service.
"""

import os
import logging
from os.path import join, dirname, abspath
try:
    from itertools import imap as map
except ImportError:  # Py 3
    pass
from datetime import datetime
from getpass import getpass
import shlex
import yaml
import json
import git
import spur
import click
from tabulate import tabulate
from . import Service, DigitalOceanNode, list_nodes, get_node, list_services, find_service

logging.basicConfig(filename='deploy.log', level=logging.WARNING,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("deploy")
logger.setLevel(logging.INFO)


do_manager = None
PROJECT_DIR = dirname(dirname(abspath(__file__)))


def load_config(name):
    with open("deployment/{}.yml".format(name)) as fp:
        config = yaml.load(fp)
    with open("deployment/{}-secrets.yml".format(name)) as fp:
        config.update(yaml.load(fp))
    return config

@click.option("--debug", is_flag=True)
@click.group()
def cli(debug):
    if debug:
        logger.setLevel(logging.DEBUG)

@cli.command()
@click.argument("service")
@click.option("--colour")
@click.option("--remote")
def build(service, colour, remote):
    """
        Build a Docker image locally and push to Docker Hub.
        or
        Build the image in a remote server using --remote option
    """

    repo = git.Repo('.', search_parent_directories=True)
    git_tag = repo.head.commit.hexsha[:7]
    if repo.is_dirty():
        git_tag += "z"

    shell = spur.LocalShell()
    config = load_config(service)
    image = config["image"]

    build_directory = os.getcwd()
    dockerfile = config["dockerfile"]
    cmd = "docker build -t {} -f {} .".format(image, dockerfile)

    # write version information
    with open(join(build_directory, "build_info.json"), "w") as fp:
        json.dump({"git": git_tag,
                   "colour": colour,
                   "date": datetime.now().isoformat()},
                   fp)

    if remote is None :
        # build image
        logger.info("Building image '{}' for service '{}', environment '{}', version {}".format(image, service, colour, git_tag))
        click.echo(f"Building image with command {cmd}")
        result = shell.run(cmd.split(), cwd=build_directory, allow_error=True)

        logger.debug(result.output)
        if result.return_code != 0:
            click.echo(result.output)
            raise click.Abort()

    else :
        #push project on remote
        logger.info("Pushing project '{}' to the remote machine {} ".format(service, remote ))

        code_dir = os.getcwd()
        node = get_node(remote)
        project_folder = code_dir.split("/")[-1]

        #clean the temp_dir if needed
        node._remote_execute('rm -R temp_dir')
        #copy the files to remote
        shell.run('scp -r -p {} root@{}:temp_dir'.format(code_dir, node.droplet.ip_address).split())

        #rename the previous project to backup

        node._remote_execute('mv {} {}_backup'.format(project_folder, project_folder))
        node._remote_execute('mv temp_dir {}'.format(project_folder))

        # build image
        logger.info("Building image '{}' for service '{}', environment '{}', version {} on remote machine {} ".format(image, service, colour, git_tag, remote))
        click.echo("Building image")

        result = node._remote_execute(cmd, cwd=project_folder)

    # tag image
    colour_tag = colour or "latest"
    for tag in (colour_tag, git_tag):
        cmd = "docker tag {} {}:{}".format(image, image, tag)
        if remote is None :
            shell.run(cmd.split())
        else :
            node._remote_execute(cmd)

    if remote is None :
        # push image
        cmd = "docker push {}:{}".format(image, colour_tag)
        click.echo("Pushing image")
        result = shell.run(cmd.split())
        logger.debug(result.output)
        logger.info("Pushed image {}:{}".format(image, colour_tag))


@cli.command()
@click.argument("service")
@click.option("--colour")
def redeploy(service, colour):
    """Redeploy a running service."""
    name = service
    if colour:
        name += "-" + colour
    service = find_service(name)
    logger.info("Redeploying '{}'".format(name))
    service.redeploy()


@cli.command()
@click.argument("service")
@click.option("--colour")
@click.option("--filename")
def log(service, colour, filename):
    """Display the log for a given service."""
    name = service
    if colour:
        name += "-" + colour
    service = find_service(name)
    if filename:
        service.logs(filename=filename)
        click.echo("Saved log to {}".format(filename))
    else:
        click.echo(service.logs())


@cli.command()
@click.argument("service")
@click.option("--colour")
def terminate(service, colour):
    """Terminate a given service."""
    name = service
    if colour:
        name += "-" + colour
    service = find_service(name)
    click.echo(service.terminate())


@click.option("-f", "--fast", is_flag=True,
              help="use cached information (faster but may not be up-to-date)")
@cli.command()
def services(fast):
    """Display a list of services."""
    def format_service(s):
        s = s.as_dict()
        s['Ports'] = ", ".join("{}:{}".format(k, v) for k, v in s['Ports'].items())
        return s
    click.echo(tabulate(map(format_service, list_services(update=not fast)),
                        headers="keys"))


@click.argument("node")
@click.argument("service")
@click.option("--colour")
@cli.command()
def launch(service, node, colour=None):
    """Launch a new service."""
    config = load_config(service)
    env_vars = config.get('env', None)
    if env_vars is None:
        env = None
    else:
        env = {}
        for var_name in env_vars:
            env[var_name] = os.getenv(var_name)
            if env[var_name] is None:
                raise Exception("Environment variable '{}' is not defined".format(var_name))
    volumes = config.get('volumes', None)
    secrets = config.get('secrets', None)
    for var_name, value in secrets.items():
        env[var_name] = value
    node_obj = get_node(node)
    name = service
    if colour:
        name += "-" + colour
        tagged_image = config['image'] + ":" + colour
    else:
        tagged_image = config['image'] + ":" + 'latest'
    service = Service(name, tagged_image, node_obj,
                      ports=config.get('ports', None),
                      env=env, volumes=volumes)
    service.launch()
    return service


@cli.group()
def node():
    """
    Sub-command for managing server nodes.
    """
    pass


@node.command('list')
def node_list():
    """Display a list of server nodes."""
    click.echo(tabulate(map(lambda s: s.as_dict(), list_nodes()),
                        headers="keys"))


@click.argument("name")
@click.option("--type", default="docker")
@click.option("--size", type=click.Choice(['s-1vcpu-1gb', 's-1vcpu-2gb', 's-2vcpu-2gb', 's-2vcpu-4gb']), default="s-1vcpu-1gb")
@node.command('create')
def node_create(name, type, size):
    """Create a new server node."""
    return DigitalOceanNode.create(name, type, size)


@click.argument("name")
@node.command('destroy')
def node_destroy(name):
    """Destroy a server node."""
    node = get_node(name)
    # todo: add an "are you sure?"
    # todo: check there are no services running on the node before shutting down
    node.destroy()



@cli.command()
def bootstrap():
    """Set-up the development and build environment."""
    pass


@cli.group()
def database():
    """Sub-command for managing database services."""
    pass


@click.argument("service")
@database.command("dump")
def db_dump(service):
    service_obj = find_service(service)
    config = load_config(service)
    params = {
        'host': service_obj.node.ip_address,
        'port': service_obj.ports['5432'],
        'timestamp': datetime.now().strftime("%Y%m%d%H%M")
    }
    db_password = config.get('secrets')['NMPI_DATABASE_PASSWORD']
    cmd = "pg_dump --clean --create --insert --host={host} --port={port} --username=nmpi_dbadmin --dbname=nmpi --file=nmpi_v2_dump_{timestamp}.sql".format(**params)
    shell = spur.LocalShell()
    shell.run(shlex.split(cmd), update_env={"PGPASSWORD": db_password})


@click.argument("filename")
@click.argument("service")
@database.command("restore")
def db_restore(service, filename):
    service_obj = find_service(service)
    config = load_config(service)
    params = {
        'host': service_obj.node.ip_address,
        'port': service_obj.ports['5432'],
        'filename': filename
    }
    db_password = config.get('secrets')['NMPI_DATABASE_PASSWORD']
    shell = spur.LocalShell()
    psql = "psql -h {host} -p {port} --username=postgres".format(**params)
    cmd = """echo "CREATE USER nmpi_dbadmin WITH PASSWORD '{}';" | """.format(db_password) + psql
    print(cmd)
    #print shlex.split(cmd)
    pg_password = getpass("Enter the password for the 'postgres' user: ")
    shell.run(["sh", "-c", cmd], update_env={"PGPASSWORD": pg_password})
    cmd = psql + " < {filename}".format(**params)
    print(cmd)
    #print shlex.split(cmd)
    shell.run(["sh", "-c", cmd], update_env={"PGPASSWORD": pg_password})


if __name__ == "__main__":
    cli()
