"""
Provides a class representing a compute node.

At present, only works with DigitalOcean Droplets and OpenStack (Nova) VMs at CSCS.
"""

import os
import sys
from collections import OrderedDict
from time import sleep
import json
import shlex
import logging
import getpass

# for Digital Ocean
import digitalocean
# for OpenStack
from keystoneauth1.identity import v3
from keystoneauth1 import session as kssession
from keystoneauth1.exceptions.auth import AuthorizationFailure
from keystoneauth1.extras._saml2 import V3Saml2Password
from keystoneclient.v3 import client as ksclient
from novaclient import client as novaclient

import yaml
import spur
from .services import Service

do_manager = None
nova_clients = None
logger = logging.getLogger("deploy")
CACHE_FILE = os.path.expanduser("~/.clouddeploycache")

with open("config.yml") as fp:
    config = yaml.safe_load(fp)
    DOCKER_USER = config["DOCKER_USER"]
    CSCS_USER = config["CSCS_USER"]
    CSCS_PROJECTS = config["CSCS_PROJECTS"]
    OS_AUTH_URL = config["OS_AUTH_URL"]
    OS_IDENTITY_PROVIDER = config["OS_IDENTITY_PROVIDER"]
    OS_IDENTITY_PROVIDER_URL = config["OS_IDENTITY_PROVIDER_URL"]
    SSH_KEYS = config["SSH_KEYS"]


def get_do_token():
    """
    Retrieve the DigitalOcean API token from the MacOS keychain.

    TODO: generalize to support Linux password stores.
    """
    if sys.platform == "darwin":
        cmd = ['security', 'find-generic-password', '-s', 'DigitalOcean API Token', '-w']
    else:
        cmd = ['pass', 'show', 'tokens/digitalocean']
    token =  spur.LocalShell().run(cmd, encoding='utf-8')
    return token.output.strip()


def get_docker_password():
    """
    Retrieve the Docker Hub password from the MacOS keychain.

    TODO: generalize to support Linux password stores.
    """
    if sys.platform == "darwin":
        cmd = "security find-internet-password -s id.docker.com -a {} -w"
    else:
        cmd = "pass show web/hub.docker.com/{}"
    pswd = spur.LocalShell().run(cmd.format(DOCKER_USER).split())
    return pswd.output.strip()


def get_nova_clients(project_names, token=None):
    username = CSCS_USER
    if token:
        auth = v3.Token(auth_url=OS_AUTH_URL, token=token)
    else:
        pwd = os.environ.get('CSCS_PASS')
        if not pwd:
            pwd = getpass.getpass("Password: ")
        auth = V3Saml2Password(auth_url=OS_AUTH_URL,
                                identity_provider=OS_IDENTITY_PROVIDER,
                                protocol='mapped',
                                identity_provider_url=OS_IDENTITY_PROVIDER_URL,
                                username=username,
                                password=pwd)

    session1 = kssession.Session(auth=auth)
    ks_client = ksclient.Client(session=session1, interface='public')
    try:
        user_id = session1.get_user_id()
    except AuthorizationFailure:
        raise Exception("Couldn't authenticate! Incorrect username.")
    except IndexError:
        raise Exception("Couldn't authenticate! Incorrect password.")
    ks_projects = {ksprj.name: ksprj
                   for ksprj in ks_client.projects.list(user=user_id)}
    clients = {}
    for project_name in project_names:
        project_id = ks_projects[project_name].id
        auth2 = v3.Token(auth_url=OS_AUTH_URL,
                        token=session1.get_token(),
                        project_id=project_id)
        session2 = kssession.Session(auth=auth2)
        clients[project_name] = novaclient.Client(version=2, session=session2)
    return clients


class Node(object):
    """
    A compute node.
    """

    def __init__(self):
        pass

    def _remote_execute(self, cmd, cwd=None):
        list_possible_keys_format = ["id_dsa", "id_rsa"]

        #check if a corresponding key can be found
        for key in list_possible_keys_format:
            if os.path.isfile(os.path.expanduser("~/.ssh/{}".format(key))) is False:
                if list_possible_keys_format[-1] == key :
                    raise Exception("No key from ~/.ssh/ matches the list_possible_keys_format {}".format(list_possible_keys_format))

        for key in list_possible_keys_format:
            shell = spur.SshShell(
                        hostname=self.ip_address, username=self.remote_username,
                        private_key_file=os.path.expanduser("~/.ssh/{}".format(key)),
                        missing_host_key=spur.ssh.MissingHostKey.warn)

            with shell:
                try :
                    result = shell.run(shlex.split(cmd), cwd=cwd, encoding="utf-8")
                    return result.output
                except :
                    pass

    @property
    def sudo_cmd(self):
        return self.use_sudo and "sudo " or ""

    def images(self):
        print(self._remote_execute(f"{self.sudo_cmd}docker images"))

    def pull(self, image):
        """
        Pull the Docker image with the given name onto this node.
        """
        logger.info("Pulling {} on {}".format(image, self.name))
        docker_password = get_docker_password()
        cmd = "f{self.sudo_cmd}docker login --username={DOCKER_USER} --password='{docker_password}'"
        result1 = self._remote_execute(cmd)
        logger.info("Logged into hub.docker.com")
        logger.debug("Pulling image {}".format(image))
        result2 = self._remote_execute("f{self.sudo_cmd}docker pull {image}")
        if "Downloaded newer image" in result2 or "Image is up to date" in result2:
            return True
        else:
            raise Exception(result2)
            return False

    def get_service(self, id):
        """
        Get information about an individual Service.
        """
        response = self._remote_execute(f"{self.sudo_cmd}docker inspect {id}")
        return Service.from_json(response, node=self)

    def services(self, show_all=False, update=True):
        """
        Return a list of Services

        :param show_all: include stopped services
        :param update: query nodes for live information about services,
                       rather than retrieving from cache.
        """
        if update or not self._have_cache:
            cmd = f"{self.sudo_cmd}docker ps -q"
            if show_all:
                cmd += " -a"
            try:
                response = self._remote_execute(cmd)
            except spur.ssh.ConnectionError as err:
                logger.warning(str(err))
                response = None
            if response:
                ids = response.strip().split("\n")
            else:
                ids = []
            services = [self.get_service(id) for id in ids]
            self._cached_services = services
        else:
            services = self._cached_services
        return services

    def terminate_service(self, id):
        response = self._remote_execute(f"{self.sudo_cmd}docker rm -f {id}")

    def rename_service(self, old_name, new_name):
        response = self._remote_execute(f"{self.sudo_cmd}docker rename {old_name} {new_name}")

    @property
    def _have_cache(self):
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as fp:
                cache = json.load(fp)
            return self.name in cache
        else:
            return False

    def _load_services_from_cache(self):
        if self._have_cache:
            with open(CACHE_FILE) as fp:
                cache = json.load(fp)
            if self.name in cache:
                return [Service(node=self, **attributes)
                        for attributes in cache[self.name]["services"]]
            else:
                return []
        else:
            return []

    def _save_services_to_cache(self, services):
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as fp:
                cache = json.load(fp)
        else:
            cache = {}
        cache[self.name] = {
            "services": [
                dict((attribute, getattr(service, attribute))
                     for attribute in ("name", "image", "status", "id", "ports", "env", "volumes"))
                for service in services]
        }
        with open(CACHE_FILE, "w") as fp:
            json.dump(cache, fp, indent=4)

    _cached_services = property(fget=_load_services_from_cache,
                                fset=_save_services_to_cache)


class DigitalOceanNode(Node):
    """
    A compute node running on Digital Ocean (a 'droplet')
    """
    remote_username = "root"
    use_sudo = False

    def __repr__(self):
        return "{}@{} [{}, {} MB, {}]".format(self.droplet.name,
                                              self.droplet.ip_address,
                                              self.droplet.status,
                                              self.droplet.size['memory'],
                                              self.droplet.region['name'])

    @property
    def name(self):
        return self.droplet.name

    @property
    def ip_address(self):
        return self.droplet.ip_address

    def as_dict(self):
        d = OrderedDict((key.title(), str(getattr(self.droplet, key)))
                        for key in ["name", "ip_address", "created_at"])
        d["Size"] = self.droplet.size['memory']
        d["Location"] = self.droplet.region['name']
        d["Type"] = self.droplet.image['name']
        d["Provider"] = "Digital Ocean"
        return d

    def show(self):
        print("Name:       " + self.droplet.name)
        print("IP address: " + str(self.droplet.ip_address))
        print("Status:     " + str(self.droplet.status))
        print("Size:       " + str(self.droplet.size['memory']) + " MB")
        print("Location:   " + self.droplet.region['name'])
        print("Type:       " + self.droplet.image['slug'])
        print("Created:    " + self.droplet.created_at)

    @classmethod
    def from_droplet(cls, droplet):
        obj = cls()
        obj.droplet = droplet
        return obj

    @classmethod
    def create(cls, name, type="docker", size="s-1vcpu-1gb"):
        # we use the name "type" for Digital Ocean images to avoid confusion with Docker images.
        global do_manager
        if do_manager is None:
            token = get_do_token()
            do_manager = digitalocean.Manager(token=token)
        new_droplet = digitalocean.Droplet(
                token=do_manager.token,
                name=name,
                region='ams2',
                image=type,
                size_slug=size,
                ssh_keys=SSH_KEYS)
        new_droplet.create()
        status = None
        while status != "completed":
            actions = new_droplet.get_actions()
            actions[0].load()
            status = actions[0].status
            sleep(10)
        running_droplet = do_manager.get_droplet(new_droplet.id)
        return cls.from_droplet(running_droplet)

    def shutdown(self):
        self.droplet.shutdown()

    def destroy(self):
        self.droplet.destroy()


class OpenStackNode(Node):
    """
    A compute node running on an OpenStack installation.

    (specifically, we support the installation at CSCS).
    """
    remote_username = "ubuntu"
    use_sudo = True

    @classmethod
    def from_nova(cls, nova_server, project_name):
        obj = cls()
        obj.nova_server = nova_server
        obj.project_name = project_name
        return obj

    def __repr__(self):
        return "{}@{} [{}, {} MB, {}]".format(self.name,
                                              self.ip_address,
                                              self.nova_server.status,
                                              self.memory,
                                              "CSCS")

    @property
    def name(self):
        return self.nova_server.name

    @property
    def ip_address(self):
        for addr in self.nova_server.addresses['int-net1']:
            if addr['OS-EXT-IPS:type'] == 'floating':
                return addr["addr"]

    @property
    def flavor(self):
        global nova_clients
        return nova_clients[CSCS_PROJECTS[0]].flavors.get(self.nova_server.flavor['id']).name

    @property
    def memory(self):
        global nova_clients
        return nova_clients[CSCS_PROJECTS[0]].flavors.get(self.nova_server.flavor['id']).ram

    @property
    def created_at(self):
        return self.nova_server.created

    def as_dict(self):
        d = OrderedDict((key.title(), str(getattr(self, key)))
                        for key in ["name", "ip_address", "created_at"])
        d["Size"] = self.memory
        d["Location"] = "CSCS"
        d["Type"] = self.flavor
        d["Provider"] = f"ICEI {self.project_name}"
        return d

    def show(self):
        print("Name:       " + self.name)
        print("IP address: " + str(self.ip_address))
        print("Status:     " + str(self.nova_server.status))
        print("Size:       " + str(self.memory) + " MB")
        print("Location:   " + "CSCS")
        print("Type:       " + self.flavor)
        print("Created:    " + self.created_at)


def list_nodes():
    global do_manager, nova_clients
    if do_manager is None:
        token = get_do_token()
        do_manager = digitalocean.Manager(token=token)
    if nova_clients is None:
        nova_clients = get_nova_clients(CSCS_PROJECTS)
    do_nodes = [DigitalOceanNode.from_droplet(droplet)
                for droplet in do_manager.get_all_droplets()]
    cscs_nodes = []
    for project_name, nova_client in nova_clients.items():
        cscs_nodes.extend([OpenStackNode.from_nova(nova_server, project_name)
                           for nova_server in nova_client.servers.list()])
    return do_nodes + cscs_nodes


def get_node(name):
    """Get a node by name."""
    all_nodes = list_nodes()
    for node in all_nodes:
        if node.name == name:
            return node
    raise Exception("No such node: {}".format(name))
