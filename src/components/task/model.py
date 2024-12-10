import json
import threading
import uuid
from concurrent.futures import Future
from hashlib import md5
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import UUID4, BaseModel, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from components._utils.process_config import process_config
from components.task import OutputFormat
from components.tool.model import Tool


class ResponseField(BaseModel):
    """
    Field class to use in the response schema for the JSON response.
    """
    title: str = Field(default=None)
    type: str = Field(default=None)
    required: bool = Field(dedault=True)


class TaskOutput(BaseModel):
    """
    Store the final output of the task in TaskOutput class.
    Depending on the task output format, use `raw`, `pydantic`, `json_dict` accordingly.
    """

    task_id: UUID4 = Field(default_factory=uuid.uuid4, frozen=True, description="store Task ID")
    raw: str = Field(default="", description="Raw output of the task")
    pydantic: Optional[BaseModel] = Field(default=None, description="Pydantic output of task")
    json_dict: Optional[Dict[str, Any]] = Field(default=None, description="JSON dictionary of task")

    def __str__(self) -> str:
        return str(self.pydantic) if self.pydantic  else str(self.json_dict) if self.json_dict else self.raw


    @property
    def json(self) -> Optional[str]:
        if self.output_format != OutputFormat.JSON:
            raise ValueError(
                """
                Invalid output format requested.
                If you would like to access the JSON output,
                pleae make sure to set the output_json property for the task
                """
            )
        return json.dumps(self.json_dict)


    def to_dict(self) -> Dict[str, Any]:
        """Convert json_output and pydantic_output to a dictionary."""
        output_dict = {}
        if self.json_dict:
            output_dict.update(self.json_dict)
        elif self.pydantic:
            output_dict.update(self.pydantic.model_dump())
        return output_dict


class Task(BaseModel):
    """
    Task to be executed by the agent or the team.
    Each task must have a description and at least one expected output format either Pydantic, Raw, or JSON.
    Then output will be stored in TaskOutput class.
    """

    __hash__ = object.__hash__

    id: UUID4 = Field(default_factory=uuid.uuid4, frozen=True, description="unique identifier for the object, not set by user")
    name: Optional[str] = Field(default=None)
    description: str = Field(description="Description of the actual task")

    # output format & output
    expected_output_raw: bool = Field(default=False)
    expected_output_json: bool = Field(default=True)
    output_json_field_list: Optional[List[ResponseField]] = Field(default=[ResponseField(title="output", type="str", required=True),])
    expected_output_pydantic: bool = Field(default=False)
    output: Optional[TaskOutput] = Field(default=None,description="store the task output aligned with the expected output format")

    # task setups
    context: Optional[List["Task"]] = Field(default=None, description="other tasks whose outputs should be used as context")
    tools: Optional[List[Tool]] = Field(default_factory=list,description="Tools the agent is limited to use for this task")
    prompt_context: Optional[str] = None
    async_execution: bool = Field(default=False,description="Whether the task should be executed asynchronously or not")
    config: Optional[Dict[str, Any]] = Field(default=None, description="Configuration for the agent")
    callback: Optional[Any] = Field(default=None, description="Callback to be executed after the task is completed.")

    # recording
    processed_by_agents: Set[str] = Field(default_factory=set)
    used_tools: int = 0
    tools_errors: int = 0
    delegations: int = 0


    @property
    def output_prompt(self):
        """
        Draft prompt for the output.
        """

        output_prompt = ""
        if self.expected_output_json == True:
            json_output = dict()
            for item in self.output_json_field_list:
                json_output[item.title] = f"your answer in {item.type}"

            output_prompt += f"""
            The output formats include the following JSON format:
            {json_output}
            """
        return output_prompt


    @property
    def expected_output_formats(self) -> List[OutputFormat]:
        outputs = []
        if self.expected_output_json:
            outputs.append(OutputFormat.JSON)
        if self.expected_output_pydantic:
            outputs.append(OutputFormat.PYDANTIC)
        if self.expected_output_raw:
            outputs.append(OutputFormat.RAW)
        return outputs


    @property
    def key(self) -> str:
        output_format = OutputFormat.JSON if self.expected_output_json == True else OutputFormat.PYDANTIC if self.expected_output_pydantic == True else OutputFormat.RAW
        source = [self.description, output_format]
        return md5("|".join(source).encode(), usedforsecurity=False).hexdigest()


    # validators
    @model_validator(mode="before")
    @classmethod
    def process_model_config(cls, values):
        return process_config(values, cls)


    @field_validator("id", mode="before")
    @classmethod
    def _deny_user_set_id(cls, v: Optional[UUID4]) -> None:
        if v:
            raise PydanticCustomError("may_not_set_field", "This field is not to be set by the user.", {})


    @model_validator(mode="after")
    def validate_required_fields(self):
        required_fields = ["description",]
        for field in required_fields:
            if getattr(self, field) is None:
                raise ValueError(f"{field} must be provided either directly or through config")
        return self


    @model_validator(mode="after")
    def set_attributes_based_on_config(self) -> "Task":
        """
        Set attributes based on the agent configuration.
        """

        if self.config:
            for key, value in self.config.items():
                setattr(self, key, value)
        return self


    @model_validator(mode="after")
    def validate_output_format(self):
        if (
            self.expected_output_json == False
            and self.expected_output_pydantic == False
            and self.expeceted_output_raw == False
        ):
            raise PydanticCustomError("Need to choose at least one output format.")
        return self

    # @model_validator(mode="after")
    # def check_tools(self):
    #     """Check if the tools are set."""
    #     if not self.tools:
    #         self.tools.extend(self.agent.tools)
    #     return self

    # @model_validator(mode="after")
    # def check_output(self):
    #     """Check if an output type is set."""
    #     output_types = [self.output_json, self.output_pydantic]
    #     if len([type for type in output_types if type]) > 1:
    #         raise PydanticCustomError(
    #             "output_type",
    #             "Only one output type can be set, either output_pydantic or output_json.",
    #             {},
    #         )
    #     return self

    def prompt(self, customer=str | None, client_business=str | None) -> str:
        """
        Return the prompt of the task.
        """
        task_slices = [
            self.description,
            f"Customer overview: {customer}",
            f"Client business overview: {client_business}",
            f"Follow the output formats: {self.output_prompt}",
        ]
        return "\n".join(task_slices)


    def _export_output(self, result: Any) -> Tuple[Optional[BaseModel], Optional[Dict[str, Any]]]:
        output_pydantic: Optional[BaseModel] = None
        output_json: Optional[Dict[str, Any]] = None
        dict_output = None

        if isinstance(result, str):
            try:
                dict_output = json.loads(result)
            except json.JSONDecodeError:
                try:
                    dict_output = eval(result)
                except:
                    try:
                        import ast
                        dict_output = ast.literal_eval(result)
                    except:
                        dict_output = None

        if self.expected_output_json:
            if isinstance(result, dict):
                output_json = result
            elif isinstance(result, BaseModel):
                output_json = result.model_dump()
            else:
                output_json = dict_output

        if self.expected_output_pydantic:
            if isinstance(result, BaseModel):
                output_pydantic = result
            elif isinstance(result, dict):
                output_json = result
            else:
                output_pydantic = None

        return output_json, output_pydantic


    def _get_output_format(self) -> OutputFormat:
        if self.output_json:
            return OutputFormat.JSON
        if self.output_pydantic:
            return OutputFormat.PYDANTIC
        return OutputFormat.RAW

    # task execution
    def execute_sync(
        self,
        agent,
        context: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
    ) -> TaskOutput:
        """Execute the task synchronously."""
        return self._execute_core(agent, context, tools)


    def execute_async(
        self,
        agent,
        context: Optional[str] = None,
        tools: Optional[List[Tool]] = None,
    ) -> Future[TaskOutput]:
        """Execute the task asynchronously."""
        future: Future[TaskOutput] = Future()
        threading.Thread(
            daemon=True,
            target=self._execute_task_async,
            args=(agent, context, tools, future),
        ).start()
        return future


    def _execute_task_async(
        self,
        agent,
        context: Optional[str],
        tools: Optional[List[Any]],
        future: Future[TaskOutput],
    ) -> None:
        """Execute the task asynchronously with context handling."""
        result = self._execute_core(agent, context, tools)
        future.set_result(result)


    def _execute_core(self, agent, context: Optional[str], tools: Optional[List[Any]]) -> TaskOutput:
        """
        Run the core execution logic of the task.
        """

        self.prompt_context = context
        tools = tools or []
        self.processed_by_agents.add(agent.role)
        result = agent.execute_task(task=self, context=context, tools=tools)
        print("Result from agent:", result)

        output_json, output_pydantic = self._export_output(result)

        task_output = TaskOutput(
            task_id=self.id,
            raw=result,
            pydantic=output_pydantic,
            json_dict=output_json,
        )
        self.output = task_output

        # self._set_end_execution_time(start_time)

        if self.callback:
            self.callback(self.output)

        # if self._execution_span:
        #     # self._telemetry.task_ended(self._execution_span, self, agent.team)
        #     self._execution_span = None

        # if self.output_file:
        #     content = (
        #         json_output
        #         if json_output
        #         else pydantic_output.model_dump_json() if pydantic_output else result
        #     )
        #     self._save_file(content)

        return task_output
