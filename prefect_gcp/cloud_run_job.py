"""
<span class="badge-api experimental"/>

Integrations with Google Cloud Run Job.

Note this module is experimental. The intefaces within may change without notice.

Examples:

    Run a job using Google Cloud Run Jobs:
    ```python
    CloudRunJob(
        image="gcr.io/my-project/my-image",
        region="us-east1",
        credentials=my_gcp_credentials
    ).run()
    ```

    Run a job that runs the command `echo hello world` using Google Cloud Run Jobs:
    ```python
    CloudRunJob(
        image="gcr.io/my-project/my-image",
        region="us-east1",
        credentials=my_gcp_credentials
        command=["echo", "hello world"]
    ).run()
    ```

"""
from __future__ import annotations

import json
import re
import time

try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal

from typing import Any, Dict, Optional
from uuid import uuid4

import googleapiclient
from anyio.abc import TaskStatus
from google.api_core.client_options import ClientOptions
from googleapiclient import discovery
from googleapiclient.discovery import Resource
from prefect.infrastructure.base import Infrastructure, InfrastructureResult
from prefect.utilities.asyncutils import run_sync_in_worker_thread, sync_compatible
from pydantic import BaseModel, Field, root_validator, validator

from prefect_gcp.credentials import GcpCredentials


class Job(BaseModel):
    """
    Utility class to call GCP `jobs` API and
    interact with the returned objects.
    """

    metadata: dict
    spec: dict
    status: dict
    name: str
    ready_condition: dict
    execution_status: dict

    def _is_missing_container(self):
        """
        Check if Job status is not ready because
        the specified container cannot be found.
        """
        if (
            self.ready_condition.get("status") == "False"
            and self.ready_condition.get("reason") == "ContainerMissing"
        ):
            return True
        return False

    def is_ready(self) -> bool:
        """Whether a job is finished registering and ready to be executed"""
        if self._is_missing_container():
            raise Exception(f"{self.ready_condition['message']}")
        return self.ready_condition.get("status") == "True"

    def has_execution_in_progress(self) -> bool:
        """See if job has a run in progress."""
        return (
            self.execution_status == {}
            or self.execution_status.get("completionTimestamp") is None
        )

    @staticmethod
    def _get_ready_condition(job: dict) -> dict:
        """Utility to access JSON field containing ready condition."""
        if job["status"].get("conditions"):
            for condition in job["status"]["conditions"]:
                if condition["type"] == "Ready":
                    return condition

        return {}

    @staticmethod
    def _get_execution_status(job: dict):
        """Utility to access JSON field containing execution status."""
        if job["status"].get("latestCreatedExecution"):
            return job["status"]["latestCreatedExecution"]

        return {}

    @classmethod
    def get(cls, client: Resource, namespace: str, job_name: str):
        """Make a get request to the GCP jobs API and return a Job instance."""
        request = client.jobs().get(name=f"namespaces/{namespace}/jobs/{job_name}")
        response = request.execute()

        return cls(
            metadata=response["metadata"],
            spec=response["spec"],
            status=response["status"],
            name=response["metadata"]["name"],
            ready_condition=cls._get_ready_condition(response),
            execution_status=cls._get_execution_status(response),
        )

    @classmethod
    def create(cls, client: Resource, namespace: str, body: dict):
        """Make a create request to the GCP jobs API."""
        request = client.jobs().create(parent=f"namespaces/{namespace}", body=body)
        response = request.execute()
        return response

    @classmethod
    def delete(cls, client: Resource, namespace: str, job_name: str):
        """Make a delete request to the GCP jobs API."""
        request = client.jobs().delete(name=f"namespaces/{namespace}/jobs/{job_name}")
        response = request.execute()
        return response

    @classmethod
    def run(cls, client: Resource, namespace: str, job_name: str):
        """Make a run request to the GCP jobs API."""
        request = client.jobs().run(name=f"namespaces/{namespace}/jobs/{job_name}")
        response = request.execute()
        return response


class Execution(BaseModel):
    """
    Utility class to call GCP `executions` API and
    interact with the returned objects.
    """

    name: str
    namespace: str
    metadata: dict
    spec: dict
    status: dict
    log_uri: str

    def is_running(self) -> bool:
        """Returns True if Execution is not completed."""
        return self.status.get("completionTime") is None

    def condition_after_completion(self):
        """Returns Execution condition if Execution has completed."""
        for condition in self.status["conditions"]:
            if condition["type"] == "Completed":
                return condition

    def succeeded(self):
        """Whether or not the Execution completed is a successful state."""
        completed_condition = self.condition_after_completion()
        if completed_condition and completed_condition["status"] == "True":
            return True

        return False

    @classmethod
    def get(cls, client: Resource, namespace: str, execution_name: str):
        """
        Make a get request to the GCP executions API
        and return an Execution instance.
        """
        request = client.executions().get(
            name=f"namespaces/{namespace}/executions/{execution_name}"
        )
        response = request.execute()

        return cls(
            name=response["metadata"]["name"],
            namespace=response["metadata"]["namespace"],
            metadata=response["metadata"],
            spec=response["spec"],
            status=response["status"],
            log_uri=response["status"]["logUri"],
        )


class CloudRunJobResult(InfrastructureResult):
    """Result from a Cloud Run Job."""


class CloudRunJob(Infrastructure):
    """
    Infrastructure block used to run GCP Cloud Run Jobs.

    Project name information is provided by the Credentials object, and should always
    be correct as long as the Credentials object is for the correct project.
    """

    _logo_url = "https://images.ctfassets.net/gm98wzqotmnx/4CD4wwbiIKPkZDt4U3TEuW/c112fe85653da054b6d5334ef662bec4/gcp.png?h=250"  # noqa
    _block_type_name = "Cloud Run Job"

    type: Literal["cloud-run-job"] = Field(
        "cloud-run-job", description="The slug for this task type."
    )
    image: str = Field(
        title="Image Name",
        description=(
            "The image to use for a new Cloud Run Job. This value must "
            "refer to an image within either Google Container Registry "
            "or Google Artifact Registry."
        ),
    )
    region: str
    credentials: GcpCredentials

    # Job settings
    cpu: Optional[int] = Field(
        title="CPU",
        description=(
            "The amount of compute allocated to the Cloud Run Job. "
            "The int must be valid based on the rules specified at "
            "https://cloud.google.com/run/docs/configuring/cpu#setting-jobs ."
        ),
    )
    memory: Optional[int] = Field(
        title="Memory",
        description="The amount of memory allocated to the Cloud Run Job.",
    )
    memory_unit: Optional[Literal["G", "Gi", "M", "Mi"]] = Field(
        title="Memory Units",
        description=(
            "The unit of memory. See "
            "https://cloud.google.com/run/docs/configuring/memory-limits#setting "
            "for additional details."
        ),
    )
    args: Optional[list[str]] = Field(
        description="Arguments to be passed to your Cloud Run Job's entrypoint command."
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to be passed to your Cloud Run Job.",
    )

    # Cleanup behavior
    keep_job: Optional[bool] = Field(
        default=False,
        title="Keep Job After Completion",
        description="Keep the completed Cloud Run Job on Google Cloud Platform.",
    )
    timeout: Optional[int] = Field(
        default=None,
        title="Job Timeout",
        description=(
            "The length of time that Prefect will wait for a Cloud Run Job to complete "
            "before raising an exception."
        ),
    )
    # For private use
    _job_name: str = None
    _execution: Optional[Execution] = None

    @property
    def job_name(self):
        """Create a unique and valid job name."""

        if self._job_name is None:
            # get `repo` from `gcr.io/<project_name>/repo/other`
            components = self.image.split("/")
            image_name = components[2]
            # only alphanumeric and '-' allowed for a job name
            modified_image_name = image_name.replace(":", "-").replace(".", "-")
            # make 50 char limit for final job name, which will be '<name>-<uuid>'
            if len(modified_image_name) > 17:
                modified_image_name = modified_image_name[:17]
            name = f"{modified_image_name}-{uuid4().hex}"
            self._job_name = name

        return self._job_name

    @property
    def memory_string(self):
        """Returns the string expected for memory resources argument."""
        if self.memory and self.memory_unit:
            return str(self.memory) + self.memory_unit
        return None

    @validator("image")
    def _remove_image_spaces(cls, value):
        """Deal with spaces in image names."""
        if value is not None:
            return value.strip()

    @validator("cpu")
    def _convert_cpu_to_k8s_quantity(cls, value):
        """Set CPU integer to the format expected by API.
        See: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
        See also: https://cloud.google.com/run/docs/configuring/cpu#setting-jobs
        """  # noqa
        return str(value * 1000) + "m"

    @root_validator
    def _check_valid_memory(cls, values):
        """Make sure memory conforms to expected values for API.
        See: https://cloud.google.com/run/docs/configuring/memory-limits#setting
        """  # noqa
        if (
            values.get("memory") is not None and values.get("memory_units") is None
        ) or (values.get("memory_units") is not None and values.get("memory") is None):
            raise ValueError(
                "A memory value and unit must both be supplied to specify a memory"
                " value other than the default memory value."
            )
        return values

    def _create_job_error(self, exc):
        """Provides a nicer error for 404s when trying to create a Cloud Run Job."""
        # TODO consider lookup table instead of the if/else,
        # also check for documented errors
        if exc.status_code == 404:
            raise RuntimeError(
                f"Failed to find resources at {exc.uri}. Confirm that region"
                f" '{self.region}' is the correct region for your Cloud Run Job and"
                f" that {self.credentials.project_id} is the correct GCP project. If"
                " your project ID is not correct, you are using a Credentials block"
                " with permissions for the wrong project."
            ) from exc

        raise exc

    def _job_run_submission_error(self, exc):
        """Provides a nicer error for 404s when submitting job runs."""
        if exc.status_code == 404:
            pat1 = r"The requested URL [^ ]+ was not found on this server"
            # pat2 = (
            #     r"Resource '[^ ]+' of kind 'JOB' in region '[\w\-0-9]+' "
            #     r"in project '[\w\-0-9]+' does not exist"
            # )
            if re.findall(pat1, str(exc)):
                raise RuntimeError(
                    f"Failed to find resources at {exc.uri}. "
                    "Confirm that region '{self.region}' is "
                    "the correct region for your Cloud Run Job "
                    "and that '{self.credentials.project_id}' is the "
                    "correct GCP project. If your project ID is not "
                    "correct, you are using a Credentials "
                    "block with permissions for the wrong project."
                ) from exc
            else:
                raise exc

        raise exc

    @sync_compatible
    async def run(self, task_status: Optional[TaskStatus] = None):
        """Run the configured job on a Google Cloud Run Job."""
        with self._get_client() as client:
            await run_sync_in_worker_thread(
                self._create_job_and_wait_for_registration, client
            )
            job_execution = await run_sync_in_worker_thread(
                self._begin_job_execution, client
            )

            if task_status:
                task_status.started(self.job_name)

            result = await run_sync_in_worker_thread(
                self._watch_job_execution_and_get_result,
                client,
                job_execution,
                5,
            )
            return result

    def _create_job_and_wait_for_registration(self, client: Resource) -> None:
        """Create a new job wait for it to finish registering."""
        try:
            self.logger.info(f"Creating Cloud Run Job {self.job_name}")
            Job.create(
                client=client,
                namespace=self.credentials.project_id,
                body=self._jobs_body(),
            )
        except googleapiclient.errors.HttpError as exc:
            self._create_job_error(exc)

        try:
            self._wait_for_job_creation(client=client)
        except Exception as exc:
            self.logger.exception(
                "Encountered an exception while waiting for job run creation"
            )
            if not self.keep_job:
                self.logger.info(
                    f"Deleting Cloud Run Job {self.job_name} from Google Cloud Run."
                )
                try:
                    Job.delete(
                        client=client,
                        namespace=self.credentials.project_id,
                        job_name=self.job_name,
                    )
                except Exception as exc:
                    self.logger.exception(
                        "Received an unexpected exception while attempting to delete"
                        f" Cloud Run Job.'{self.job_name}':\n{exc!r}"
                    )
            raise exc

    def _begin_job_execution(self, client: Resource) -> Execution:
        """Submit a job run for execution and return the execution object."""
        try:
            self.logger.info(f"Submitting Cloud Run Job {self.job_name} for execution.")
            submission = Job.run(
                client=client,
                namespace=self.credentials.project_id,
                job_name=self.job_name,
            )

            job_execution = Execution.get(
                client=client,
                namespace=submission["metadata"]["namespace"],
                execution_name=submission["metadata"]["name"],
            )

            command = (
                " ".join(self.command)
                if self.command
                else "'default container command'"
            )

            self.logger.info(
                f"Cloud Run Job {self.job_name!r}: Running command '{command!r}'"
            )
        except Exception as exc:
            self._job_run_submission_error(exc)

        return job_execution

    def _watch_job_execution_and_get_result(
        self, client: Resource, execution: Execution, poll_interval: int
    ) -> CloudRunJobResult:
        """Wait for execution to complete and then return result."""
        try:
            job_execution = self._watch_job_execution(
                client=client,
                job_execution=execution,
                timeout=self.timeout,
                poll_interval=poll_interval,
            )
        except Exception as exc:
            self.logger.exception(
                "Received an unexpected exception while monitoring Cloud Run Job"
                f" '{self.job_name!r}':\n{exc!r}"
            )
            raise

        if job_execution.succeeded():
            status_code = 0
            self.logger.info(f"Job Run {self.job_name} completed successfully")
        else:
            status_code = 1
            error_msg = job_execution.condition_after_completion()["message"]
            self.logger.error(
                f"Job Run {self.job_name} did not complete successfully. {error_msg}"
            )

        self.logger.info(
            f"Job Run logs can be found on GCP at: {job_execution.log_uri}"
        )

        if not self.keep_job:
            self.logger.info(
                f"Deleting completed Cloud Run Job {self.job_name} from Google Cloud"
                " Run..."
            )
            try:
                Job.delete(
                    client=client,
                    namespace=self.credentials.project_id,
                    job_name=self.job_name,
                )
            except Exception as exc:
                self.logger.exception(
                    "Received an unexpected exception while attempting to delete Cloud"
                    f" Run Job.'{self.job_name}':\n{exc!r}"
                )

        return CloudRunJobResult(identifier=self.job_name, status_code=status_code)

    def _jobs_body(self) -> dict:
        """Create properly formatted body used for a Job CREATE request.
        See: https://cloud.google.com/run/docs/reference/rest/v1/namespaces.jobs
        """
        jobs_metadata = {
            "name": self.job_name,
            "annotations": {
                # See: https://cloud.google.com/run/docs/troubleshooting#launch-stage-validation  # noqa
                "run.googleapis.com/launch-stage": "BETA"
            },
        }

        # env and command here
        containers = [self._add_container_settings({"image": self.image})]

        body = {
            "apiVersion": "run.googleapis.com/v1",
            "kind": "Job",
            "metadata": jobs_metadata,
            "spec": {  # JobSpec
                "template": {  # ExecutionTemplateSpec
                    "spec": {  # ExecutionSpec
                        "template": {  # TaskTemplateSpec
                            "spec": {"containers": containers}  # TaskSpec
                        }
                    },
                }
            },
        }
        return body

    def preview(self) -> str:
        """Generate a preview of the job definition that will be sent to GCP." """
        body = self._jobs_body()

        return json.dumps(body, indent=2)

    def _watch_job_execution(
        self, client, job_execution: Execution, timeout: int, poll_interval: int = 5
    ):
        """
        Update job_execution status until it is no longer running or timeout is reached.
        """
        t0 = time.time()
        while job_execution.is_running():
            elapsed_time = time.time() - t0
            if timeout is not None and elapsed_time > timeout:
                raise RuntimeError(
                    f"Timed out after {elapsed_time}s while waiting for Cloud Run Job "
                    "execution to complete. Your job may still be running on GCP."
                )

            time.sleep(poll_interval)

            job_execution = Execution.get(
                client=client,
                namespace=job_execution.namespace,
                execution_name=job_execution.name,
            )

        return job_execution

    def _wait_for_job_creation(self, client: Resource, poll_interval: int = 5):
        """Give created job time to register."""
        job = Job.get(
            client=client, namespace=self.credentials.project_id, job_name=self.job_name
        )

        while not job.is_ready():
            ready_condition = (
                job.ready_condition
                if job.ready_condition
                else "waiting for condition update"
            )
            self.logger.info(
                f"Job is not yet ready... Current condition: {ready_condition}"
            )
            time.sleep(poll_interval)
            job = Job.get(
                client=client,
                namespace=self.credentials.project_id,
                job_name=self.job_name,
            )

    def _get_client(self) -> Resource:
        """Get the base client needed for interacting with GCP APIs."""
        # region needed for 'v1' API
        api_endpoint = f"https://{self.region}-run.googleapis.com"
        gcp_creds = self.credentials.get_credentials_from_service_account()
        options = ClientOptions(api_endpoint=api_endpoint)

        return discovery.build(
            "run", "v1", client_options=options, credentials=gcp_creds
        ).namespaces()

    # CONTAINER SETTINGS
    def _add_container_settings(self, base_settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add settings related to containers for Cloud Run Jobs to a dictionary.
        Includes environment variables, entrypoint command, entrypoint arguments,
        and cpu and memory limits.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        and https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        """  # noqa
        container_settings = base_settings.copy()
        container_settings.update(self._add_env())
        container_settings.update(self._add_resources())
        container_settings.update(self._add_command())
        container_settings.update(self._add_args())
        return container_settings

    def _add_args(self) -> dict:
        """Set the arguments that will be passed to the entrypoint for a Cloud Run Job.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """  # noqa
        return {"args": self.args} if self.args else {}

    def _add_command(self) -> dict:
        """Set the command that a container will run for a Cloud Run Job.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container
        """  # noqa
        return {"command": self.command}

    def _add_resources(self) -> dict:
        """Set specified resources limits for a Cloud Run Job.
        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#ResourceRequirements
        See also: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
        """  # noqa
        resources = {"limits": {}, "requests": {}}

        if self.cpu is not None:
            resources["limits"]["cpu"] = self.cpu
            resources["requests"]["cpu"] = self.cpu
        if self.memory_string is not None:
            resources["limits"]["memory"] = self.memory_string
            resources["requests"]["memory"] = self.memory_string

        return {"resources": resources} if resources["requests"] else {}

    def _add_env(self) -> dict:
        """Add environment variables for a Cloud Run Job.

        Method `self._base_environment()` gets necessary Prefect environment variables
        from the config.

        See: https://cloud.google.com/run/docs/reference/rest/v1/Container#envvar for
        how environment variables are specified for Cloud Run Jobs.
        """  # noqa
        env = {**self._base_environment(), **self.env}
        cloud_run_job_env = [{"name": k, "value": v} for k, v in env.items()]
        return {"env": cloud_run_job_env}
