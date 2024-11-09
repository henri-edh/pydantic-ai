import json
import re
from collections.abc import Iterable
from dataclasses import asdict
from datetime import timezone

import pydantic_core
import pytest
from dirty_equals import IsStr
from inline_snapshot import snapshot
from pydantic import BaseModel

from pydantic_ai import Agent, CallContext, ModelRetry
from pydantic_ai.messages import (
    LLMMessage,
    LLMResponse,
    LLMToolCalls,
    Message,
    SystemPrompt,
    ToolCall,
    ToolReturn,
    UserPrompt,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.result import Cost
from tests.conftest import IsNow

pytestmark = pytest.mark.anyio


def return_last(messages: list[Message], _: AgentInfo) -> LLMMessage:
    last = messages[-1]
    response = asdict(last)
    response.pop('timestamp', None)
    response['message_count'] = len(messages)
    return LLMResponse(' '.join(f'{k}={v!r}' for k, v in response.items()))


def test_simple():
    agent = Agent(FunctionModel(return_last), deps=None)
    result = agent.run_sync('Hello')
    assert result.response == snapshot("content='Hello' role='user' message_count=1")
    assert result.all_messages() == snapshot(
        [
            UserPrompt(
                content='Hello',
                timestamp=IsNow(tz=timezone.utc),
                role='user',
            ),
            LLMResponse(
                content="content='Hello' role='user' message_count=1",
                timestamp=IsNow(tz=timezone.utc),
                role='llm-response',
            ),
        ]
    )

    result2 = agent.run_sync('World', message_history=result.all_messages())
    assert result2.response == snapshot("content='World' role='user' message_count=3")
    assert result2.all_messages() == snapshot(
        [
            UserPrompt(
                content='Hello',
                timestamp=IsNow(tz=timezone.utc),
                role='user',
            ),
            LLMResponse(
                content="content='Hello' role='user' message_count=1",
                timestamp=IsNow(tz=timezone.utc),
                role='llm-response',
            ),
            UserPrompt(
                content='World',
                timestamp=IsNow(tz=timezone.utc),
                role='user',
            ),
            LLMResponse(
                content="content='World' role='user' message_count=3",
                timestamp=IsNow(tz=timezone.utc),
                role='llm-response',
            ),
        ]
    )


def weather_model(messages: list[Message], info: AgentInfo) -> LLMMessage:  # pragma: no cover
    assert info.allow_text_result
    assert info.retrievers.keys() == {'get_location', 'get_weather'}
    last = messages[-1]
    if last.role == 'user':
        return LLMToolCalls(
            calls=[
                ToolCall.from_json(
                    'get_location',
                    json.dumps({'location_description': last.content}),
                )
            ]
        )
    elif last.role == 'tool-return':
        if last.tool_name == 'get_location':
            return LLMToolCalls(calls=[ToolCall.from_json('get_weather', last.model_response_str())])
        elif last.tool_name == 'get_weather':
            location_name = next(m.content for m in messages if m.role == 'user')
            return LLMResponse(f'{last.content} in {location_name}')

    raise ValueError(f'Unexpected message: {last}')


weather_agent: Agent[None, str] = Agent(FunctionModel(weather_model))


@weather_agent.retriever_plain
async def get_location(location_description: str) -> str:
    if location_description == 'London':
        lat_lng = {'lat': 51, 'lng': 0}
    else:
        lat_lng = {'lat': 0, 'lng': 0}
    return json.dumps(lat_lng)


@weather_agent.retriever_context
async def get_weather(_: CallContext[None], lat: int, lng: int):
    if (lat, lng) == (51, 0):
        # it always rains in London
        return 'Raining'
    else:
        return 'Sunny'


def test_weather():
    result = weather_agent.run_sync('London')
    assert result.response == 'Raining in London'
    assert result.all_messages() == snapshot(
        [
            UserPrompt(
                content='London',
                timestamp=IsNow(tz=timezone.utc),
                role='user',
            ),
            LLMToolCalls(
                calls=[ToolCall.from_json('get_location', '{"location_description": "London"}')],
                timestamp=IsNow(tz=timezone.utc),
                role='llm-tool-calls',
            ),
            ToolReturn(
                tool_name='get_location',
                content='{"lat": 51, "lng": 0}',
                timestamp=IsNow(tz=timezone.utc),
                role='tool-return',
            ),
            LLMToolCalls(
                calls=[
                    ToolCall.from_json(
                        'get_weather',
                        '{"lat": 51, "lng": 0}',
                    )
                ],
                timestamp=IsNow(tz=timezone.utc),
                role='llm-tool-calls',
            ),
            ToolReturn(
                tool_name='get_weather',
                content='Raining',
                timestamp=IsNow(tz=timezone.utc),
                role='tool-return',
            ),
            LLMResponse(
                content='Raining in London',
                timestamp=IsNow(tz=timezone.utc),
                role='llm-response',
            ),
        ]
    )

    result = weather_agent.run_sync('Ipswich')
    assert result.response == 'Sunny in Ipswich'


def call_function_model(messages: list[Message], _: AgentInfo) -> LLMMessage:  # pragma: no cover
    last = messages[-1]
    if last.role == 'user':
        if last.content.startswith('{'):
            details = json.loads(last.content)
            return LLMToolCalls(
                calls=[
                    ToolCall.from_json(
                        details['function'],
                        json.dumps(details['arguments']),
                    )
                ]
            )
    elif last.role == 'tool-return':
        return LLMResponse(pydantic_core.to_json(last).decode())

    raise ValueError(f'Unexpected message: {last}')


var_args_agent = Agent(FunctionModel(call_function_model), deps=123)


@var_args_agent.retriever_context
def get_var_args(ctx: CallContext[int], *args: int):
    assert ctx.deps == 123
    return json.dumps({'args': args})


def test_var_args():
    result = var_args_agent.run_sync('{"function": "get_var_args", "arguments": {"args": [1, 2, 3]}}')
    response_data = json.loads(result.response)
    # Can't parse ISO timestamps with trailing 'Z' in older versions of python:
    response_data['timestamp'] = re.sub('Z$', '+00:00', response_data['timestamp'])
    assert response_data == snapshot(
        {
            'tool_name': 'get_var_args',
            'content': '{"args": [1, 2, 3]}',
            'tool_id': None,
            'timestamp': IsStr() & IsNow(iso_string=True, tz=timezone.utc),
            'role': 'tool-return',
        }
    )


def call_retriever(messages: list[Message], info: AgentInfo) -> LLMMessage:
    if len(messages) == 1:
        assert len(info.retrievers) == 1
        retriever_id = next(iter(info.retrievers.keys()))
        return LLMToolCalls(calls=[ToolCall.from_json(retriever_id, '{}')])
    else:
        return LLMResponse('final response')


def test_deps_none():
    agent = Agent(FunctionModel(call_retriever), deps=None)

    @agent.retriever_context
    async def get_none(ctx: CallContext[None]):
        nonlocal called

        called = True
        assert ctx.deps is None
        return ''

    called = False
    agent.run_sync('Hello')
    assert called

    called = False
    agent.run_sync('Hello', deps=None)
    assert called


def test_deps_init():
    def get_check_foobar(ctx: CallContext[tuple[str, str]]) -> str:
        nonlocal called

        called = True
        assert ctx.deps == ('foo', 'bar')
        return ''

    agent = Agent(FunctionModel(call_retriever), deps=('foo', 'bar'))
    agent.retriever_context(get_check_foobar)
    called = False
    agent.run_sync('Hello')
    assert called

    agent: Agent[tuple[str, str], str] = Agent(FunctionModel(call_retriever))
    agent.retriever_context(get_check_foobar)
    called = False
    agent.run_sync('Hello', deps=('foo', 'bar'))
    assert called


def test_model_arg():
    agent = Agent(deps=None)
    result = agent.run_sync('Hello', model=FunctionModel(return_last))
    assert result.response == snapshot("content='Hello' role='user' message_count=1")

    with pytest.raises(RuntimeError, match='`model` must be set either when creating the agent or when calling it.'):
        agent.run_sync('Hello')


agent_all = Agent(deps=None)


@agent_all.retriever_context
async def foo(_: CallContext[None], x: int) -> str:
    return str(x + 1)


@agent_all.retriever_context(retries=3)
def bar(ctx, x: int) -> str:  # pyright: ignore[reportUnknownParameterType,reportMissingParameterType]
    return str(x + 2)


@agent_all.retriever_plain
async def baz(x: int) -> str:
    return str(x + 3)


@agent_all.retriever_plain(retries=1)
def qux(x: int) -> str:
    return str(x + 4)


@agent_all.retriever_plain  # pyright: ignore[reportUnknownArgumentType]
def quz(x) -> str:  # pyright: ignore[reportUnknownParameterType,reportMissingParameterType]
    return str(x)  # pyright: ignore[reportUnknownArgumentType]


@agent_all.system_prompt
def spam() -> str:
    return 'foobar'


def test_register_all():
    def f(messages: list[Message], info: AgentInfo) -> LLMMessage:
        return LLMResponse(
            f'messages={len(messages)} allow_text_result={info.allow_text_result} retrievers={len(info.retrievers)}'
        )

    result = agent_all.run_sync('Hello', model=FunctionModel(f))
    assert result.response == snapshot('messages=2 allow_text_result=True retrievers=5')


def test_call_all():
    result = agent_all.run_sync('Hello', model=TestModel())
    assert result.response == snapshot('{"foo":"1","bar":"2","baz":"3","qux":"4","quz":"a"}')
    assert result.all_messages() == snapshot(
        [
            SystemPrompt(content='foobar'),
            UserPrompt(content='Hello', timestamp=IsNow(tz=timezone.utc)),
            LLMToolCalls(
                calls=[
                    ToolCall.from_object('foo', {'x': 0}),
                    ToolCall.from_object('bar', {'x': 0}),
                    ToolCall.from_object('baz', {'x': 0}),
                    ToolCall.from_object('qux', {'x': 0}),
                    ToolCall.from_object('quz', {'x': 'a'}),
                ],
                timestamp=IsNow(tz=timezone.utc),
            ),
            ToolReturn(tool_name='foo', content='1', timestamp=IsNow(tz=timezone.utc)),
            ToolReturn(tool_name='bar', content='2', timestamp=IsNow(tz=timezone.utc)),
            ToolReturn(tool_name='baz', content='3', timestamp=IsNow(tz=timezone.utc)),
            ToolReturn(tool_name='qux', content='4', timestamp=IsNow(tz=timezone.utc)),
            ToolReturn(tool_name='quz', content='a', timestamp=IsNow(tz=timezone.utc)),
            LLMResponse(
                content='{"foo":"1","bar":"2","baz":"3","qux":"4","quz":"a"}', timestamp=IsNow(tz=timezone.utc)
            ),
        ]
    )


def test_retry_str():
    call_count = 0

    def try_again(messages: list[Message], _: AgentInfo) -> LLMMessage:
        nonlocal call_count
        call_count += 1

        return LLMResponse(str(call_count))

    agent = Agent(FunctionModel(try_again), deps=None)

    @agent.result_validator
    async def validate_result(r: str) -> str:
        if r == '1':
            raise ModelRetry('Try again')
        else:
            return r

    result = agent.run_sync('')
    assert result.response == snapshot('2')


def test_retry_result_type():
    call_count = 0

    def try_again(messages: list[Message], _: AgentInfo) -> LLMMessage:
        nonlocal call_count
        call_count += 1

        return LLMToolCalls(calls=[ToolCall.from_object('final_result', {'x': call_count})])

    class Foo(BaseModel):
        x: int

    agent = Agent(FunctionModel(try_again), result_type=Foo, deps=None)

    @agent.result_validator
    async def validate_result(r: Foo) -> Foo:
        if r.x == 1:
            raise ModelRetry('Try again')
        else:
            return r

    result = agent.run_sync('')
    assert result.response == snapshot(Foo(x=2))


def stream_text_function(_messages: list[Message], _: AgentInfo) -> Iterable[str]:
    yield 'hello '
    yield 'world'


async def test_stream_text():
    agent = Agent(FunctionModel(stream_function=stream_text_function), deps=None)
    async with agent.run_stream('') as result:
        assert await result.get_response() == snapshot('hello world')
        assert result.all_messages() == snapshot(
            [
                UserPrompt(content='', timestamp=IsNow(tz=timezone.utc)),
                LLMResponse(content='hello world', timestamp=IsNow(tz=timezone.utc)),
            ]
        )
        assert result.cost() == snapshot(Cost())


class Foo(BaseModel):
    x: int


async def test_stream_structure():
    def stream_structured_function(_messages: list[Message], agent_info: AgentInfo) -> Iterable[DeltaToolCalls]:
        assert agent_info.result_tools is not None
        assert len(agent_info.result_tools) == 1
        name = agent_info.result_tools[0].name
        yield {0: DeltaToolCall(name=name)}
        yield {0: DeltaToolCall(args='{"x": ')}
        yield {0: DeltaToolCall(args='1}')}

    agent = Agent(FunctionModel(stream_function=stream_structured_function), deps=None, result_type=Foo)
    async with agent.run_stream('') as result:
        assert await result.get_response() == snapshot(Foo(x=1))
        assert result.cost() == snapshot(Cost())


async def test_pass_neither():
    with pytest.raises(TypeError, match='Either `function` or `stream_function` must be provided'):
        FunctionModel()  # pyright: ignore[reportCallIssue]


async def test_return_empty():
    agent = Agent(FunctionModel(stream_function=lambda _, __: []), deps=None)
    with pytest.raises(ValueError, match='Stream function must return at least one item'):
        async with agent.run_stream(''):
            pass