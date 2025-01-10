import json
import logging
import os
import subprocess

import requests
from common import utils
from django.db import models
from django.utils.translation import gettext_lazy as _
from lxml import etree

from .location import Location

# Core Django, alphabetical

LOGGER = logging.getLogger(__name__)


class Archipelago(models.Model):
    """Integration with Archipelago using the REST API."""

    space = models.OneToOneField("Space", to_field="uuid", on_delete=models.CASCADE)

    archipelago_url = models.URLField(
        max_length=256,
        verbose_name=_("Archipelago URL"),
        help_text=_("Archipelago URL"),
    )

    archipelago_user = models.CharField(
        max_length=64,
        verbose_name=_("Archipelago username"),
        help_text=_("Archipelago username for authentication"),
    )

    archipelago_password = models.CharField(
        max_length=64,
        verbose_name=_("Archipelago password"),
        help_text=_("Archipelago password for authentication"),
    )

    class Meta:
        verbose_name = _("Archipelago via REST API")
        app_label = "locations"

    ALLOWED_LOCATION_PURPOSE = [Location.AIP_STORAGE]

    def read_metadata_json(self, input_path):
        """Reads metadata.json file from the transfer location."""
        metadata_json_path = os.path.join(os.path.dirname(input_path), "metadata.json")
        if not os.path.exists(metadata_json_path):
            LOGGER.info("No metadata.json file found.")
            return {}

        try:
            with open(metadata_json_path) as metadata_file:
                metadata = json.load(metadata_file)
                LOGGER.info("Metadata.json content: %s", metadata)
                os.remove(metadata_json_path)
                LOGGER.info("metadata.json file deleted.")
                return metadata
        except Exception as e:
            LOGGER.error("Error reading metadata.json: %s", str(e))
            return {}

    def _upload_file(self, filename, source_path):
        """Uploads zip file to Archipelago before creating new entity
        so if upload fails, new entity not created"""
        url = self.archipelago_url + "/jsonapi/node/aip/field_file_drop"
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'file; filename="{filename}"',
        }
        try:
            with open(source_path, "rb") as file:
                response = requests.post(
                    url,
                    data=file,
                    headers=headers,
                    auth=(self.archipelago_user, self.archipelago_password),
                )
                response.raise_for_status()

            if response.status_code == 201:
                LOGGER.info("AIP file uploaded successfully!")
                response_json = response.json()
                fid = (
                    response_json.get("data", {})
                    .get("attributes", {})
                    .get("drupal_internal__fid")
                )
                return fid
            else:
                LOGGER.error(
                    f"File upload failed with status code {response.status_code}: {response.text}"
                )
        except (OSError, requests.exceptions.RequestException) as e:
            LOGGER.error("Error during AIP upload to archipelago %s", str(e))

    def _upload_tsm(self, title, source_path):
        command = [
            "rsync",
            "-z",
            source_path,
            "lacddt@dp-tsm-staging.is.ed.ac.uk:~/staging/",
        ]
        LOGGER.info(command)
        LOGGER.info("about to upload to TSM")
        try:
            out = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ).communicate()
            LOGGER.info(out)
            command = [
                'ssh lacddt@dp-tsm-staging.is.ed.ac.uk "echo {} > ~/staging/{}.done"'.format(
                    title, source_path.split("/")[-1][:-3]
                )
            ]
            out = subprocess.Popen(
                command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ).communicate()
            LOGGER.info(out)
        except OSError as err:
            raise Exception(f"Could not run {command[0]}: {err}.")
        except subprocess.CalledProcessError as err:
            raise Exception(
                "Could not archive {} using {}: {}.".format(
                    source_path, command[0], err
                )
            )

    def extract_title_from_mets_xml(self, xml_string):
        """Retrieves title from METs file or creates title from file name"""
        try:
            root = etree.fromstring(xml_string)
            namespaces = utils.NSMAP
            title_element = root.find(
                ".//mets:dmdSec/mets:mdWrap/mets:xmlData/dcterms:dublincore/dc:title",
                namespaces=namespaces,
            )
            if title_element is not None:
                return title_element.text.strip()
        except Exception as e:
            LOGGER.error("Error extracting title from METS XML: %s", str(e))
        return None

    def merge_dc_metadata(self, dc_fields, metadata_json):
        """Merge dc fields from METS XML and metadata.json."""
        for key, value in metadata_json.items():
            if key.startswith("dc."):
                # Convert 'dc.title' to 'field_title'
                field_name = "field_" + key.split(".")[1]
                dc_fields[field_name] = value

        # Handle the collection field for ismemberof
        if "dc.collection_nid" in metadata_json:
            dc_fields["ismemberof"] = metadata_json["dc.collection_nid"]

        # Adding the entity mapping to the strawberry
        dc_fields["ap:entitymapping"] = {
            "entity:file": [
                "model",
                "audios",
                "images",
                "videos",
                "documents",
                "upload_associated_warcs",
            ],
            "entity:node": ["ispartof", "ismemberof"],
        }

        return dc_fields

    def get_dc_metadata(self, xml_string, input_path, metadata_json_path):
        """Extracts Dublin Core metadata from METS file"""
        try:
            root = etree.fromstring(xml_string)
            namespaces = utils.NSMAP
            dc_fields = {}
            for dc_element in root.findall(".//dc:*", namespaces=namespaces):
                field_name = dc_element.tag.split("}")[-1]
                if field_name == "title":
                    continue
                appended_field = "field_" + field_name
                field_value = dc_element.text.strip() if dc_element.text else None
                LOGGER.info(
                    f"dc value added which is {field_value} where the field is {appended_field}"
                )
                dc_fields[appended_field] = field_value
            output_dir = os.path.dirname(input_path) + "/extracted/"
            os.makedirs(output_dir, exist_ok=True)
            metadata_json = self.read_metadata_json(metadata_json_path)
            dc_fields = self.merge_dc_metadata(dc_fields, metadata_json)
            strawberry = json.dumps(dc_fields)

            LOGGER.info(f"Merged complete strawberry json is {strawberry}")

            # Write the merged metadata back into the XML (updating DC fields)
            for dc_element in root.findall(".//dc:*", namespaces=namespaces):
                field_name = dc_element.tag.split("}")[-1]
                if field_name in dc_fields:
                    new_value = dc_fields.get("field_" + field_name)
                    if new_value:
                        dc_element.text = new_value

            # Save the updated XML back to the original input path
            updated_xml = etree.tostring(
                root, pretty_print=True, encoding="utf-8"
            ).decode("utf-8")
            with open(input_path, "w", encoding="utf-8") as file:
                file.write(updated_xml)

            LOGGER.info(f"Merged complete strawberry json is {strawberry}")
            try:
                subprocess.Popen(
                    ["rm", "-rf", output_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except Exception as cleanup_error:
                LOGGER.warning(
                    "Failed to clean up extracted files: %s", str(cleanup_error)
                )
            return strawberry

        except Exception as e:
            LOGGER.error("Error extracting dc fields %s", str(e))
        return None

    @staticmethod
    def _get_files(package_type, output_dir, input_path, dirname, aip_uuid):
        """Locate, extract (if necessary), and return the METS XML element and metadata.json file path
        for this package.
        """
        if package_type == "AIP":
            # Define paths for METS file and metadata.json
            relative_mets_path = os.path.join(
                dirname, "data", "METS." + str(aip_uuid) + ".xml"
            )
            relative_metadata_path = os.path.join(
                dirname, "data", "objects", "metadata.json"
            )

            # Define full paths for extraction
            mets_path = os.path.join(output_dir, relative_mets_path)
            metadata_path = os.path.join(output_dir, relative_metadata_path)

            # Extraction command
            command = [
                "unar",
                "-force-overwrite",
                "-o",
                output_dir,
                input_path,
                relative_mets_path,
                relative_metadata_path,
            ]

            try:
                # Run the extraction process
                subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                ).communicate()

                # Parse the METS XML
                mets_el = etree.parse(mets_path)

                # Remove the extracted METS file to clean up
                os.remove(mets_path)

                # Check if the metadata.json exists at the expected location
                if not os.path.exists(metadata_path):
                    raise FileNotFoundError(
                        f"metadata.json not found at expected location: {metadata_path}"
                    )

                return mets_el, metadata_path

            except subprocess.CalledProcessError as err:
                raise Exception(f"Could not extract files from {input_path}: {err}.")

    def _get_metadata(self, input_path, aip_uuid, package_type):
        """Extracts METS.xml from AIP"""
        output_dir = os.path.dirname(input_path) + "/"
        dirname = os.path.splitext(os.path.basename(input_path))[0]
        mets_el, metadata_json_path = self._get_files(
            package_type, output_dir, input_path, dirname, aip_uuid
        )
        if mets_el is None:
            LOGGER.error("Failed to get METS element")
            return None
        return etree.tostring(mets_el), metadata_json_path

    def _upload_metadata(self, fid, strawberry, title):
        """POSTs metadata via JSON API to create new entity on archipelago containing the file
        and metadata"""
        LOGGER.info("uploading metadata")
        url = self.archipelago_url + "/jsonapi/node/aip"
        archivematica_zip_link = {
            "label": title,
            "archivematica_zip": fid,  # links new aip entity to uploaded zip file
            "ap:entitymapping": {
                "entity:file": ["archivematica_zip"],
                "entity:node": ["ispartof", "ismemberof"],
            },
        }
        headers = {"Content-Type": "application/vnd.api+json"}
        strawberry_dict = json.loads(strawberry)
        combined_data = {
            **strawberry_dict,
            **archivematica_zip_link,
        }  # these are our strawberry field
        json_metadata = json.dumps(combined_data)
        request_data = {
            "data": {
                "type": "AIP",
                "attributes": {
                    "title": title,
                    "field_descriptive_metadata": json_metadata,
                },
            }
        }
        json_data = json.dumps(request_data)
        try:
            response = requests.post(
                url,
                data=json_data,
                headers=headers,
                auth=(self.archipelago_user, self.archipelago_password),
            )
            response.raise_for_status()

            if response.status_code == 201:
                LOGGER.info("AIP entity created successfully!")
            else:
                LOGGER.error(
                    f"File upload failed with status code {response.status_code}: {response.text}"
                )
        except (OSError, requests.exceptions.RequestException) as e:
            LOGGER.error("Error during AIP upload to archipelago %s", str(e))

    def move_from_storage_service(self, source_path, destination_path, package=None):
        """Moves self.staging_path/src_path to dest_path."""
        if package is None:
            raise Exception("Archipelago requires package param.")
        LOGGER.info(
            "source_path: %s, destination_path: %s, package: %s",
            source_path,
            destination_path,
            package,
        )
        LOGGER.info(f"source path is {source_path}")
        field_uuid = package.uuid
        mets_xml, metadata_json_path = self._get_metadata(
            source_path, field_uuid, package_type="AIP"
        )
        title = self.extract_title_from_mets_xml(mets_xml)
        filename = os.path.basename(source_path)
        if title is None:  # use transfer name if title was not defined in metadata.
            parts = filename.split("-")
            if len(parts) < 2:
                title = "Default title for Archipelago AIP"
            else:
                title = parts[0]  # splitting title from uuid
        LOGGER.info(f"field uuid is {field_uuid}")
        try:
            fid = self._upload_file(filename, source_path)
            LOGGER.info(f"fid found to be {fid}")
            LOGGER.info("NOW UPLOADING TO TSM")
            self._upload_tsm(title, source_path)
            LOGGER.info(f"SOURCE PATH IS found to be {source_path}")
            LOGGER.info(f"DESTINATION PATH found to be {destination_path}")
            strawberry = self.get_dc_metadata(
                mets_xml, source_path, metadata_json_path
            )  # getting other dublic core metadata fields
            if strawberry is not None:
                try:
                    strawberry_dict = json.loads(strawberry)
                    strawberry_dict["aip_uuid"] = str(field_uuid)
                    self._upload_metadata(fid, json.dumps(strawberry_dict), title)
                except Exception as e:
                    LOGGER.error(
                        "could not upload metadata (make aip entity to archipelago): %s",
                        str(e),
                    )
            else:
                LOGGER.info("strawberry was not found")
        except Exception as e:
            LOGGER.error("could not upload AIP file: %s", str(e))

    def browse(self, path):
        raise NotImplementedError("Archipelago does not implement browse")

    def delete_path(self, delete_path):
        raise NotImplementedError("Archipelago does not implement browse")

    def move_to_storage_service(self, src_path, dest_path, dest_space):
        raise NotImplementedError("Archipelago does not implement browse")
