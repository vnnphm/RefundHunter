import os
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, StartSessionContent,
    TextContent, chat_protocol_spec,
)

load_dotenv()

agent = Agent(
    name="refund-hunter",
    seed=os.environ["AGENT_SEED"],  # keep fixed: it sets the agent's address
    port=8001,
    mailbox=True,
)
chat = Protocol(spec=chat_protocol_spec)


def reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])


@chat.on_message(ChatMessage)
async def on_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    for item in msg.content:
        if isinstance(item, TextContent):
            ctx.logger.info(f"text from {sender}: {item.text[:120]}")
            await ctx.send(sender, reply(f"Refund Hunter here. You said: {item.text}"))
        elif isinstance(item, StartSessionContent):
            ctx.logger.info(f"session started by {sender}")
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"session ended by {sender}")
        else:
            ctx.logger.info(f"ignoring {type(item).__name__} from {sender}")


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ack from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
