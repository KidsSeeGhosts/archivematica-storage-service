import cgi
import datetime
import logging
import os
import shutil
import tempfile
import time

import requests
from common.utils import generate_checksum
from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _
from locations import models
from locations.models.async_manager import AsyncManager


LOGGER = logging.getLogger(__name__)


def get_deposit(uuid):
    """
    Shortcut to retrieve deposit data. Returns deposit model object or None
    """
    try:
        return models.Package.objects.get(uuid=uuid)
    except models.Package.DoesNotExist:
        return None


def deposit_list(location_uuid):
    """
    Retrieve list of deposits

    Returns list containing all deposits in the Location with `location_uuid`.
    """
    # TODO: filter out completed ones?
    deposits = (
        models.Package.objects.filter(package_type=models.Package.DEPOSIT)
        .filter(current_location_id=location_uuid)
        .exclude(status=models.Package.DELETED)
        .exclude(status=models.Package.FINALIZED)
    )
    return deposits


def write_request_body_to_temp_file(request):
    """
    Write HTTP request's body content to a temp file.

    Return the temp file's path
    """
    _, temp_filepath = tempfile.mkstemp()
    with open(temp_filepath, "ab") as f:
        f.write(request.body)
    return temp_filepath


def parse_filename_from_content_disposition(header):
    """
    Parse a filename from HTTP Content-Disposition data

    Return filename
    """
    _, params = cgi.parse_header(header)
    filename = params.get("filename", "")
    return filename


def pad_destination_filepath_if_it_already_exists(filepath, original=None, attempt=0):
    """
    Generate unique file paths.

    Pad a filename numerically, preserving the file extension, if it's a duplicate of an existing file. This function is recursive.

    Returns padded (if necessary) file path
    """
    if original is None:
        original = filepath
    attempt = attempt + 1
    if os.path.exists(filepath):
        return pad_destination_filepath_if_it_already_exists(
            original + "_" + str(attempt), original, attempt
        )
    return filepath


def download_resource(
    url, destination_path, filename=None, username=None, password=None
):
    """
    Download a resource.

    Download a URL resource to a destination directory, using the response's Content-Disposition header, if available, to determine the destination filename (using the filename at the end of the URL otherwise)

    Returns filename of downloaded resource
    """
    LOGGER.info("downloading url: %s", url)

    auth = None
    if username is not None and password is not None:
        auth = (username, password)

    verify = not settings.INSECURE_SKIP_VERIFY
    response = requests.get(url, auth=auth, verify=verify)
    if filename is None:
        if "content-disposition" in response.headers:
            filename = parse_filename_from_content_disposition(
                response.headers["content-disposition"]
            )
        else:
            filename = os.path.basename(url)
    LOGGER.info("Filename set to " + filename)

    filepath = os.path.join(destination_path, filename)
    with open(filepath, "wb") as fp:
        fp.write(response.content)

    return filename


def deposit_download_tasks(deposit):
    """
    Return a deposit's download tasks.
    """
    return models.PackageDownloadTask.objects.filter(package=deposit)


def deposit_downloading_status(deposit):
    """
    Return deposit status, indicating whether any incomplete or failed batch
    downloads exist.
    """
    tasks = deposit_download_tasks(deposit)
    # Check each task for completion and failure
    # If any are incomplete or have failed, then return that status
    # If all are complete (or there are no tasks), return completed
    for task in tasks:
        status = task.downloading_status()
        if status != models.PackageDownloadTask.COMPLETE:
            # Status is either models.PackageDownloadTask.FAILED or INCOMPLETE
            return status
    return models.PackageDownloadTask.COMPLETE


def spawn_download_task(deposit_uuid, objects, subdir=None):
    """
    Spawn an asynchrnous batch download
    """
    AsyncManager.run_task(_fetch_content, deposit_uuid, objects, subdir)


def _fetch_content(deposit_uuid, objects, subdirs=None):
    """
    Download a number of files, keeping track of progress and success using a
    database record. After downloading, finalize deposit if requested.

    If subdirs is provided, the file will be moved into a subdirectory of the
    new transfer; otherwise, it will be placed in the transfer's root.
    """
    # add download task to keep track of progress
    deposit = get_deposit(deposit_uuid)
    task = models.PackageDownloadTask(package=deposit)
    task.downloads_attempted = len(objects)
    task.downloads_completed = 0
    task.save()

    # Get deposit protocol info
    deposit_space = deposit.current_location.space.get_child_space()
    fedora_username = getattr(deposit_space, "fedora_user", None)
    fedora_password = getattr(deposit_space, "fedora_password", None)

    # download the files
    temp_dir = tempfile.mkdtemp()
    completed = 0
    for item in objects:
        # create download task file record
        task_file = models.PackageDownloadTaskFile(task=task)
        task_file.save()

        try:
            filename = item["filename"]

            task_file.filename = filename
            task_file.url = item["url"]
            task_file.save()

            download_resource(
                url=item["url"],
                destination_path=temp_dir,
                filename=filename,
                username=fedora_username,
                password=fedora_password,
            )

            temp_filename = os.path.join(temp_dir, filename)

            if (
                item["checksum"] is not None
                and item["checksum"]
                != generate_checksum(temp_filename, "md5").hexdigest()
            ):
                os.unlink(temp_filename)
                raise Exception(_("Incorrect checksum"))

            # Some MODS records have no proper filenames
            if filename == "MODS Record":
                filename = item["object_id"].replace(":", "-") + "-MODS.xml"

            if subdirs:
                base_path = os.path.join(deposit.full_path, *subdirs)
            else:
                base_path = deposit.full_path

            new_path = os.path.join(base_path, filename)
            shutil.move(temp_filename, new_path)

            # mark download task file record complete or failed
            task_file.completed = True
            task_file.save()

            LOGGER.info("Saved file to " + new_path)
            completed += 1

            file_record = models.File(
                name=item["filename"],
                source_id=item["object_id"],
                checksum=generate_checksum(new_path, "sha512").hexdigest(),
            )
            file_record.save()
        except Exception as e:
            LOGGER.exception("Package download task encountered an error:" + str(e))
            # an error occurred
            task_file.failed = True
            task_file.save()

    # remove temp dir
    shutil.rmtree(temp_dir)

    # record the number of successful downloads and completion time
    task.downloads_completed = completed
    task.download_completion_time = timezone.now()
    task.save()

    # if the deposit is ready for finalization and this is the last batch
    # download to complete, then finalize
    ready_for_finalization = deposit.misc_attributes.get(
        "ready_for_finalization", False
    )
    if (
        ready_for_finalization
        and deposit_downloading_status(deposit) == models.PackageDownloadTask.COMPLETE
    ):
        _finalize_if_not_empty(deposit_uuid)


def spawn_finalization(deposit_uuid):
    """
    Spawn an asynchronous finalization
    """
    AsyncManager.run_task(_finalize_if_not_empty, deposit_uuid)


def _finalize_if_not_empty(deposit_uuid):
    """
    Approve a deposit for processing and mark is as completed or finalization failed

    Returns a dict of the form:
    {
        'error': <True|False>,
        'message': <description of success or failure>
    }
    """
    deposit = get_deposit(deposit_uuid)
    completed = False
    result = {"error": True, "message": _("Deposit empty, or not done downloading.")}
    # don't finalize if still downloading
    if deposit_downloading_status(deposit) == models.PackageDownloadTask.COMPLETE:
        if len(os.listdir(deposit.full_path)) > 0:
            # get sword server so we can access pipeline information
            if not deposit.current_location.pipeline.exists():
                return {
                    "error": True,
                    "message": _("No Pipeline associated with this collection"),
                }
            pipeline = deposit.current_location.pipeline.all()[0]
            result = activate_transfer_and_request_approval_from_pipeline(
                deposit, pipeline
            )
            if result.get("error", False):
                LOGGER.warning("Error creating transfer: %s", result)
            else:
                completed = True

    if completed:
        # mark deposit as complete
        deposit.misc_attributes.update({"deposit_completion_time": timezone.now()})
        deposit.status = models.Package.FINALIZED
    else:
        # make finalization as having failed
        deposit.misc_attributes.update({"finalization_attempt_failed": True})
    deposit.save()

    return result


def activate_transfer_and_request_approval_from_pipeline(deposit, pipeline):
    """
    Handle requesting the approval of a transfer from a pipeline via a REST call.

    This function returns a dict representation of the results, either returning
    the JSON returned by the request to the pipeline (converted to a dict) or
    a dict indicating a pipeline authentication issue.

    The dict representation is of the form:
    {
        'error': <True|False>,
        'message': <description of success or failure>
    }
    """
    # make sure pipeline API access is configured
    attrs = ("remote_name", "api_username", "api_key")
    if not all([getattr(pipeline, attr, None) for attr in attrs]):
        missing_attrs = [a for a in attrs if not getattr(pipeline, a, None)]
        return {
            "error": True,
            "message": _("Pipeline properties %(properties)s not set.")
            % {"properties": ", ".join(missing_attrs)},
        }

    # TODO: add error if more than one location is returned
    processing_location = models.Location.objects.get(
        pipeline=pipeline, purpose=models.Location.CURRENTLY_PROCESSING
    )

    destination_path = os.path.join(
        processing_location.full_path,
        "watchedDirectories",
        "activeTransfers",
        "standardTransfer",
        deposit.current_path,
    )

    # FIXME this should use Space.move_[to|from]_storage_service
    # move to standard transfers directory
    destination_path = pad_destination_filepath_if_it_already_exists(destination_path)
    shutil.move(deposit.full_path, destination_path)

    # Find the path of the transfer that we want to approve.
    while True:
        try:
            results = pipeline.list_unapproved_transfers()
        except Exception:
            LOGGER.exception("Retrieval of unapproved transfers failed")
        else:
            directories = [
                result["directory"]
                for result in results["results"]
                if result["type"] == "standard"
            ]
            if deposit.current_path in directories:
                break
        time.sleep(5)

    # Approve transfer.
    try:
        results = pipeline.approve_transfer(
            deposit.current_path, transfer_type="standard"
        )
    except Exception:
        LOGGER.exception(
            "Automatic approval of transfer for deposit %s failed", deposit.uuid
        )
        # Move back to deposit directory. FIXME: moving the files out form under
        # Archivematica leaves a transfer that will always error out - leave it?
        shutil.move(destination_path, deposit.full_path)
        return {
            "error": True,
            "message": _(
                "Request to pipeline %(uuid)s transfer approval API "
                "failed: check credentials and REST API IP "
                "allowlist."
            )
            % {"uuid": pipeline.uuid},
        }
    return results


def sword_error_response(request, status, summary):
    """Generate SWORD 2.0 error response"""
    error_details = {"summary": summary, "status": status}
    error_details["request"] = request
    error_details["update_time"] = datetime.datetime.now().__str__()
    error_details["user_agent"] = request.headers["user-agent"]
    error_xml = render_to_string("locations/api/sword/error.xml", error_details)
    return HttpResponse(error_xml, status=error_details["status"])


def store_mets_data(mets_path, deposit, object_id):
    """
    Create transfer directory structure & store METS in it.

    Creates submission documentation directory with MODS and objects subdirectories.
    Also moves the METS into the submission documentation directory, overwriting anything already there.
    """
    create_dirs = [
        os.path.join(deposit.full_path, "submissionDocumentation", "mods"),
        os.path.join(deposit.full_path, object_id.replace(":", "-")),
    ]
    for d in create_dirs:
        try:
            LOGGER.debug("Creating %s", d)
            os.makedirs(d)
        except OSError:
            if not os.path.isdir(d):
                raise

    mets_name = object_id.replace(":", "-") + "-METS.xml"
    target = os.path.join(deposit.full_path, "submissionDocumentation", mets_name)

    # There may be a previous METS file if the same file is being
    # re-transferred, so remove and update the METS in this case.
    if os.path.exists(target):
        LOGGER.debug("Removing existing %s", target)
        os.unlink(target)

    LOGGER.debug("Move METS file from %s to %s", mets_path, target)
    shutil.move(mets_path, target)
