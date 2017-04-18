"""
Provides a class representing a compute node.

At present, only works with DigitalOcean Droplets.
"""

import os
import sys
from collections import OrderedDict
from time import sleep
import json
import shlex
import logging
import digitalocean
import spur
from .services import Service

do_manager = None
logger = logging.getLogger("deploy")
CACHE_FILE = os.path.expanduser("~/.clouddeploycache")
DOCKER_USER = "cnrsunic"


def get_token():
    """
    Retrieve the DigitalOcean API token from the MacOS keychain.

    TODO: generalize to support Linux password stores.
    """
    if sys.platform == "darwin":
        cmd = ['security', 'find-generic-password', '-s', 'DigitalOcean API token', '-w']
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
        cmd = "security find-internet-password -s hub.docker.com -a {} -w"
    else:
        cmd = "pass show web/hub.docker.com/{}"
    pswd = spur.LocalShell().run(cmd.format(DOCKER_USER).split())
    return pswd.output.strip()


class Node(object):
    """
    A compute node.
    """

    def __init__(self):
        pass

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
        d["Region"] = self.droplet.region['name']
        return d

    def show(self):
        print("Name:       " + self.droplet.name)
        print("IP address: " + str(self.droplet.ip_address))
        print("Status:     " + str(self.droplet.status))
        print("Size:       " + str(self.droplet.size['memory']) + " MB")
        print("Region:     " + self.droplet.region['name'])
        print("Type:       " + self.droplet.image['slug'])
        print("Created:    " + self.droplet.created_at)

    @classmethod
    def from_droplet(cls, droplet):
        obj = cls()
        obj.droplet = droplet
        return obj

    @classmethod
    def create(cls, name, type="docker", size="512mb"):
        # we use the name "type" for Digital Ocean images to avoid confusion with Docker images.
        global do_manager
        if do_manager is None:
            token = get_token()
            do_manager = digitalocean.Manager(token=token)
        new_droplet = digitalocean.Droplet(
                token=do_manager.token,
                name=name,
                region='ams2',
                image=type,
                size_slug=size,
                ssh_keys=['66:0b:b5:20:a0:68:f9:fc:82:5a:de:c1:ce:03:4f:84'])
        new_droplet.create()
        status = None
        while status != "completed":
            actions = new_droplet.get_actions()
            actions[0].load()
            status = actions[0].status
            sleep(10)
        running_droplet = do_manager.get_droplet(new_droplet.id)
        return cls.from_droplet(running_droplet)

    def _remote_execute(self, cmd, cwd=None):
        shell = spur.SshShell(
                    hostname=self.droplet.ip_address, username="root",
                    private_key_file=os.path.expanduser("~/.ssh/id_dsa"),  # to generalize - could be id_rsa, etc.
                    missing_host_key=spur.ssh.MissingHostKey.warn)
        with shell:
            result = shell.run(shlex.split(cmd), cwd=cwd, encoding="utf-8")
            return result.output

    def images(self):
        print(self._remote_execute("docker images"))

    def pull(self, image):
        """
        Pull the Docker image with the given name onto this node.
        """
        logger.info("Pulling {} on {}".format(image, self.name))
        docker_password = get_docker_password()
        cmd = "docker login --username={} --password='{}'".format(DOCKER_USER, docker_password)
        result1 = self._remote_execute("docker login --username={} --password='{}' hub.docker.com".format(DOCKER_USER, docker_password))
        logger.info("Logged into hub.docker.com")
        logger.debug("Pulling image {}".format(image))
        result2 = self._remote_execute("docker pull {}".format(image))
        if "Downloaded newer image" in result2 or "Image is up to date" in result2:
            return True
        else:
            raise Exception(result2)
            return False

    def get_service(self, id):
        """
        Get information about an individual Service.
        """
        response = self._remote_execute("docker inspect {}".format(id))
        return Service.from_json(response, node=self)

    def services(self, show_all=False, update=True):
        """
        Return a list of Services

        :param show_all: include stopped services
        :param update: query nodes for live information about services,
                       rather than retrieving from cache.
        """
        if update or not self._have_cache:
            cmd = "docker ps -q"
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
        response = self._remote_execute("docker rm -f {}".format(id))

    def rename_service(self, old_name, new_name):
        response = self._remote_execute("docker rename {} {}".format(old_name, new_name))

    def shutdown(self):
        self.droplet.shutdown()

    def destroy(self):
        self.droplet.destroy()

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


def list_nodes():
    global do_manager
    if do_manager is None:
        token = get_token()
        do_manager = digitalocean.Manager(token=token)
    return [Node.from_droplet(droplet)
            for droplet in do_manager.get_all_droplets()]


def get_node(name):
    """Get a node by name."""
    all_nodes = list_nodes()
    for node in all_nodes:
        if node.name == name:
            return node
    raise Exception("No such node: {}".format(name))
