from .provider import Provider, Image
from .vault import EC2Credential
from dateutil.parser import parse
import boto3
import re
import time
import logging

logger = logging.getLogger(__name__)


class EC2(Provider):
    __instances = dict()

    def __new__(cls, vault_namespace):
        if vault_namespace not in EC2.__instances:
            EC2.__instances[vault_namespace] = self = object.__new__(cls)
            self.__credentials = EC2Credential(vault_namespace)
            self.__ec2_client = dict()
            self.__eks_client = dict()
            self.__ec2_resource = dict()
            self.__secret = None
            self.__key = None

        EC2.__instances[vault_namespace].check_credentials()
        return EC2.__instances[vault_namespace]

    def check_credentials(self):
        if self.__credentials.isExpired():
            self.__credentials.renew()
            self.__key = None
            self.__secret = None
            self.__ec2_resource = dict()
            self.__ec2_client = dict()
            self.__eks_client = dict()

        self.__secret = self.__credentials.getData('secret_key')
        self.__key = self.__credentials.getData('access_key')

        for i in range(1, 60 * 5):
            try:
                self.all_regions()
                return True
            except Exception:
                logger.info("check_credentials (attemp:%d) with key %s expiring at %s ",
                            i, self.__key, self.__credentials.getAuthExpire())
                time.sleep(1)
        self.all_regions()

    def ec2_resource(self, region='eu-central-1'):
        if region not in self.__ec2_resource:
            self.__ec2_resource[region] = boto3.resource('ec2', aws_access_key_id=self.__key,
                                                         aws_secret_access_key=self.__secret,
                                                         region_name=region)
        return self.__ec2_resource[region]

    def ec2_client(self, region='eu-central-1'):
        if region not in self.__ec2_client:
            self.__ec2_client[region] = boto3.client('ec2', aws_access_key_id=self.__key,
                                                     aws_secret_access_key=self.__secret,
                                                     region_name=region)
        return self.__ec2_client[region]

    def eks_client(self, region='eu-central-1'):
        if region not in self.__eks_client:
            self.__eks_client[region] = boto3.client('eks', aws_access_key_id=self.__key,
                                                     aws_secret_access_key=self.__secret,
                                                     region_name=region)
        return self.__eks_client[region]

    def all_clusters(self):
        regions = self.all_regions()
        clusters = list()
        for region in regions:
            response = self.eks_client(region).list_clusters()
            [clusters.append(cluster) for cluster in response['clusters']]
        return clusters

    def list_instances(self, region='eu-central-1'):
        return [i for i in self.ec2_resource(region).instances.all()]

    def all_regions(self):
        regions_resp = self.ec2_client().describe_regions()
        regions = [region['RegionName'] for region in regions_resp['Regions']]
        return regions

    def delete_instance(self, instance_id):
        instances_list = list(self.ec2_resource().instances.filter(InstanceIds=[instance_id]))
        if len(instances_list) > 0:
            instances_list[0].terminate()
        else:
            logger.warning("Instance {} is ACTIVE in local DB but does not exists on EC2".format(instance_id))

    def parse_image_name(self, img_name):
        regexes = [
            # openqa-SLES12-SP5-EC2.x86_64-0.9.1-BYOS-Build1.55.raw.xz
            re.compile(r'''^openqa-SLES
                              (?P<version>\d+(-SP\d+)?)
                              -(?P<flavor>EC2)
                              \.
                              (?P<arch>[^-]+)
                              -
                              (?P<kiwi>\d+\.\d+\.\d+)
                              -
                              (?P<type>(BYOS|On-Demand))
                              -Build
                              (?P<build>\d+\.\d+)
                              \.raw\.xz
                              ''', re.RegexFlag.X),
            # openqa-SLES15-SP2.x86_64-0.9.3-EC2-HVM-Build1.10.raw.xz'
            # openqa-SLES15-SP2-BYOS.x86_64-0.9.3-EC2-HVM-Build1.10.raw.xz'
            # openqa-SLES15-SP2.aarch64-0.9.3-EC2-HVM-Build1.49.raw.xz'
            re.compile(r'''^openqa-SLES
                              (?P<version>\d+(-SP\d+)?)
                              (-(?P<type>[^\.]+))?
                              \.
                              (?P<arch>[^-]+)
                              -
                              (?P<kiwi>\d+\.\d+\.\d+)
                              -
                              (?P<flavor>EC2[-\w]*)
                              -Build
                              (?P<build>\d+\.\d+)
                              \.raw\.xz
                              ''', re.RegexFlag.X),
            # openqa-SLES12-SP4-EC2-HVM-BYOS.x86_64-0.9.2-Build2.56.raw.xz'
            re.compile(r'''^openqa-SLES
                              (?P<version>\d+(-SP\d+)?)
                              -
                              (?P<flavor>EC2[^\.]+)
                              \.
                              (?P<arch>[^-]+)
                              -
                              (?P<kiwi>\d+\.\d+\.\d+)
                              -
                              Build
                              (?P<build>\d+\.\d+)
                              \.raw\.xz
                              ''', re.RegexFlag.X)
        ]
        return self.parse_image_name_helper(img_name, regexes)

    def cleanup_all(self):
        response = self.ec2_client().describe_images(Owners=['self'])
        images = list()
        for img in response['Images']:
            # img is in the format described here:
            # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_images
            m = self.parse_image_name(img['Name'])
            if m:
                logger.debug("[{}]Image {} is candidate for deletion with build {}".format(
                    self.__credentials.namespace, img['Name'], m['build']))
                images.append(Image(img['Name'], flavor=m['key'], build=m['build'], date=parse(img['CreationDate']),
                                    img_id=img['ImageId']))
            else:
                logger.error("[{}] Unable to parse image name '{}'".format(self.__credentials.namespace, img['Name']))

        keep_images = self.get_keeping_image_names(images)

        for img in [i for i in images if i.name not in keep_images]:
            logger.info("Delete image '{}' (ami:{})".format(img.name, img.id))
            self.ec2_client().deregister_image(ImageId=img.id, DryRun=False)
