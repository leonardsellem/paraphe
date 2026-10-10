# Telegram private-bot callback lifetime and stale-button rules

Research note for Paraphe map #1.
API facts only. No live bot was created. Retrieved 2026-08-25 from official Telegram pages (Bot API 10.3, dated 24 August 2026 on the Bot API page).

Question: for a dedicated Telegram bot in a private chat, how long do inline buttons stay valid, can a message be edited in place when a card is cancelled or expires, and what must the spec require so a late tap cannot approve a dead card?

ADR 0002 already chose the private bot (`docs/adr/0002-telegram-private-bot-tap-surface.md`). This note does not reopen that decision. ADR 0007 later superseded the original card-id-plus-action encoding guidance: production `callback_data` must bind the card id and current version (or an equivalent revision nonce).

## Short answer

Telegram does **not** document a lifetime for inline callback buttons themselves. A `callback_data` button stays on the message until the bot edits or deletes that message. A tap can still produce a `callback_query` long after Paraphe's card is dead. Telegram will not refuse the tap for "card expired."

The Bot API **does** document:

- Incoming updates (including `callback_query`) are kept on Telegram's servers at most **24 hours** if the bot has not received them ([Getting updates](https://core.telegram.org/bots/api#getting-updates)).
- `callback_data` is **1–64 bytes** ([InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)).
- The bot **must** call `answerCallbackQuery` after every tap, even with no user-visible text ([CallbackQuery](https://core.telegram.org/bots/api#callbackquery), [answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)).
- Bot-sent private-chat messages **can be edited in place**, including text and the inline keyboard. The 48-hour edit caveat on `editMessageText` / `editMessageReplyMarkup` is written for **business messages that were not sent by the bot and do not contain an inline keyboard**, not for ordinary bot messages in a private chat ([Updating messages](https://core.telegram.org/bots/api#updating-messages), [editMessageText](https://core.telegram.org/bots/api#editmessagetext)).
- Deleting a message is limited to **48 hours** after send ([deleteMessage](https://core.telegram.org/bots/api#deletemessage)). Edit-in-place is the durable way to take buttons off a card.

The spec must treat Telegram as a tap **transport**, not as the card's authority. A late tap must be answered with a stale toast/alert, must not approve, and must be checked against Paraphe's own card id + live state. Removing or rewriting the keyboard is UX, not the safety gate.

## Sources

Primary pages retrieved for this note:

| Page | URL | Role |
|---|---|---|
| Telegram Bot API | https://core.telegram.org/bots/api | Canonical HTTP Bot API (Bot API 10.3) |
| Bot features — Inline Keyboards | https://core.telegram.org/bots/features#inline-keyboards | Product description of inline keyboards |
| Bots FAQ | https://core.telegram.org/bots/faq | Private-chat message delivery; update plumbing |
| Bot API changelog | https://core.telegram.org/bots/api-changelog | Historical 48-hour channel-edit exception; `MaybeInaccessibleMessage` |
| Bot buttons (MTProto) | https://core.telegram.org/api/bots/buttons | Official callback flow, timeouts, `setBotCallbackAnswer` |
| `messages.getBotCallbackAnswer` | https://core.telegram.org/method/messages.getBotCallbackAnswer | Client-side timeout / invalid-message errors |
| `messages.setBotCallbackAnswer` | https://core.telegram.org/method/messages.setBotCallbackAnswer | `QUERY_ID_INVALID` |
| `messages.editMessage` | https://core.telegram.org/method/messages.editMessage | `MESSAGE_EDIT_TIME_EXPIRED`, `BUTTON_DATA_INVALID` |
| `keyboardButtonCallback` | https://core.telegram.org/constructor/keyboardButtonCallback | MTProto callback button constructor |

Secondary community write-ups were not used.

## 1. What a tap is

An inline keyboard is attached to a message and shown next to it ([InlineKeyboardMarkup](https://core.telegram.org/bots/api#inlinekeyboardmarkup); [features: Inline Keyboards](https://core.telegram.org/bots/features#inline-keyboards)). Pressing a callback button does **not** send a chat message. It sends `callback_data` to the bot as a `callback_query` update ([InlineKeyboardButton.callback_data](https://core.telegram.org/bots/api#inlinekeyboardbutton); [Update.callback_query](https://core.telegram.org/bots/api#update); [CallbackQuery](https://core.telegram.org/bots/api#callbackquery)).

`CallbackQuery` fields that matter for Paraphe:

- `id` — unique identifier for **this** query. Required later as `answerCallbackQuery.callback_query_id`.
- `from` — the tapping user.
- `message` — `MaybeInaccessibleMessage`: the bot-sent message that held the button, **or** an `InaccessibleMessage` if that message was deleted / is otherwise inaccessible ([CallbackQuery.message](https://core.telegram.org/bots/api#callbackquery); [MaybeInaccessibleMessage](https://core.telegram.org/bots/api#maybeinaccessiblemessage); [InaccessibleMessage](https://core.telegram.org/bots/api#inaccessiblemessage)).
- `data` — the button's `callback_data`. Telegram warns: "Be aware that the message originated the query can contain no callback buttons with this data" ([CallbackQuery.data](https://core.telegram.org/bots/api#callbackquery)).

That last sentence is the documented hole: the query payload can outlive the keyboard currently shown on the message (the bot edited the markup; the client was stale; the message is inaccessible). The bot still receives `data`.

On the MTProto side, a user tap is `messages.getBotCallbackAnswer`; Telegram then delivers `updateBotCallbackQuery` to the bot; the bot must reply with `messages.setBotCallbackAnswer` ([Bot buttons — Sending / Answering a callback query](https://core.telegram.org/api/bots/buttons)). The HTTP methods `getUpdates`/`answerCallbackQuery` are the Bot API view of the same flow.

## 2. How long do inline buttons stay valid?

### Buttons on the message: no documented TTL

The Bot API does **not** give a lifetime, expiry, or "too old" rule for `InlineKeyboardButton` / `callback_data` on a message the bot sent. There is no field on `InlineKeyboardMarkup` for "valid until." There is no documented automatic disable of callback buttons after N hours.

What **is** documented:

- The keyboard "appears right next to the message it belongs to" ([InlineKeyboardMarkup](https://core.telegram.org/bots/api#inlinekeyboardmarkup)).
- Clients keep showing it until the message is edited or deleted. Telegram's own feature page tells bots to **edit the keyboard** when state changes rather than send a new message ([features: Inline Keyboards](https://core.telegram.org/bots/features#inline-keyboards)).
- A `DisabledButton` type exists ("the button is disabled and does nothing") but is a constructor the bot must send; Telegram does not auto-apply it ([InlineKeyboardButton.disabled](https://core.telegram.org/bots/api#inlinekeyboardbutton); [DisabledButton](https://core.telegram.org/bots/api#disabledbutton)).

**Not documented (do not specify as Telegram behaviour):** visual buttons disappearing on their own after card expiry; Telegram refusing a tap because Paraphe's card expired; a private-chat vs channel difference for callback-button lifetime.

### The query id, not the button, is the short-lived object

`answerCallbackQuery` takes `callback_query_id` ([answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)). The MTProto twin `messages.setBotCallbackAnswer` returns `400 QUERY_ID_INVALID` / "The query ID is invalid" ([messages.setBotCallbackAnswer](https://core.telegram.org/method/messages.setBotCallbackAnswer)). The Bot API page does not publish that error string, but it does require answering **this** query id after the user presses the button, and it documents a client-side progress bar until that answer arrives ([CallbackQuery NOTE](https://core.telegram.org/bots/api#callbackquery)).

Official pages do **not** state how many seconds a `callback_query.id` remains answerable. They do state:

- Bots "must reply to the query as quickly as possible" ([Bot buttons — Answering a callback query](https://core.telegram.org/api/bots/buttons)).
- `messages.setBotCallbackAnswer` "must be called anyway, even if no message or url is returned, to avoid timeouts on the client" ([same](https://core.telegram.org/api/bots/buttons)).
- The user client can fail with `400 BOT_RESPONSE_TIMEOUT` / "A timeout occurred while fetching data from the bot" and `-503 Timeout` / "Timeout while fetching data" when the bot does not answer ([messages.getBotCallbackAnswer](https://core.telegram.org/method/messages.getBotCallbackAnswer)).
- `answerCallbackQuery.cache_time` is only "the maximum amount of time in seconds that the result of the callback query may be cached client-side. Defaults to 0" ([answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)). It is a client cache of **this answer**, not a button TTL.

So: Telegram times out the **answer to one tap** if the bot is slow or offline. It does not time out the **button**.

### Updates themselves: 24 hours on the server

"Incoming updates are stored on the server until the bot receives them either way, but they will not be kept longer than 24 hours" ([Getting updates](https://core.telegram.org/bots/api#getting-updates)). `callback_query` is one field of `Update` ([Update](https://core.telegram.org/bots/api#update)). If Paraphe is down for more than 24 hours, a tap that Telegram accepted from the client may never be delivered. That is a delivery bound, not a button-valid bound. A later tap on the same still-visible button can generate a **new** `callback_query` with a new `id`.

## 3. Can the message be edited in place when a card is cancelled or expires?

Yes, for a bot-sent message in a private chat.

[Updating messages](https://core.telegram.org/bots/api#updating-messages):

> The following methods allow you to change an existing message in the message history instead of sending a new one with a result of an action. This is most useful for messages with inline keyboards using callback queries, but can also help reduce clutter in conversations with regular chat bots.
>
> Please note, that it is currently only possible to edit messages without reply_markup or with inline keyboards.

That is the documented pattern: edit the card instead of posting a second message.

Relevant methods, all taking `chat_id` + `message_id` (private chat) or `inline_message_id` (inline mode, out of scope for ADR 0002):

| Method | What it changes | 48-hour note on the Bot API page |
|---|---|---|
| [editMessageText](https://core.telegram.org/bots/api#editmessagetext) | Text (or rich content) and optional new `reply_markup` | "business messages that were not sent by the bot and do not contain an inline keyboard can only be edited within 48 hours from the time they were sent" |
| [editMessageReplyMarkup](https://core.telegram.org/bots/api#editmessagereplymarkup) | Keyboard only | Same 48-hour business-message caveat |
| [editMessageCaption](https://core.telegram.org/bots/api#editmessagecaption) / [editMessageMedia](https://core.telegram.org/bots/api#editmessagemedia) | Caption / media + optional keyboard | Same caveat |

The 48-hour sentence is scoped to **business** messages **not sent by the bot** **without** an inline keyboard. A Paraphe card is a message **sent by the bot**, with an inline keyboard, in a private chat. The Bot API page does **not** attach a 48-hour edit limit to that case.

Related documented limits that **do** apply:

- [deleteMessage](https://core.telegram.org/bots/api#deletemessage): "A message can only be deleted if it was sent less than 48 hours ago." Also: "Bots can delete outgoing messages in private chats, groups, and supergroups." After 48 hours, delete is not the tool; edit remains the documented way to change text/keyboard.
- MTProto `messages.editMessage` can return `400 MESSAGE_EDIT_TIME_EXPIRED` / "You can't edit this message anymore, too much time has passed since its creation" ([messages.editMessage](https://core.telegram.org/method/messages.editMessage)). The Bot API page does **not** copy that error onto `editMessageText` for bot-sent private messages. The spec should not invent a Bot API hour count from the MTProto string. It should require handling edit failure (log, leave the visual buttons, still refuse the tap in Paraphe).
- Channel exception, historical: Bot API 4.3 (31 May 2019) allowed channel admins with `can_edit_messages` to call `editMessageReplyMarkup` on **other administrators'** messages "forever without the 48 hours limit" ([changelog, 31 May 2019](https://core.telegram.org/bots/api-changelog#may-31-2019)). That is a **channel / other-author** rule. It is not the private-bot path.

`editMessageReplyMarkup.reply_markup` is optional. Passing a new `InlineKeyboardMarkup` replaces the keyboard. The Bot API does not spell out "omit `reply_markup` to strip all buttons"; the safe, documented move is to send a new markup: empty rows, `DisabledButton`s, or no callback buttons. Combine with `editMessageText` so the card body says cancelled / expired / already tapped.

Telegram will not hide the old buttons for you when Paraphe expires the card. If the bot does not edit, the buttons stay on screen and remain tappable.

## 4. Expired / invalid callback, and whether buttons persist visually

### What Telegram will still do after Paraphe's card is dead

If the message still has callback buttons, a tap still creates a `callback_query`. Official docs do not say the client greys the button out, or that Telegram checks a server-side card state.

If the bot already edited the message and removed callback buttons, a late tap from a stale client can still arrive. Telegram already warns that `CallbackQuery.data` may not match any current button on the message ([CallbackQuery.data](https://core.telegram.org/bots/api#callbackquery)). `CallbackQuery.message` may be an `InaccessibleMessage` (`date` always `0`) if the message was deleted ([InaccessibleMessage](https://core.telegram.org/bots/api#inaccessiblemessage)). Bot API 7.0 (21 December 2023) added those types specifically to document this for `CallbackQuery.message` ([changelog, 21 December 2023](https://core.telegram.org/bots/api-changelog#december-21-2023)).

### What Telegram will refuse

Documented failures are about **answering this query** or **editing this message**, not about "card expired":

| Surface | Error / behaviour | Meaning for Paraphe |
|---|---|---|
| [messages.setBotCallbackAnswer](https://core.telegram.org/method/messages.setBotCallbackAnswer) | `400 QUERY_ID_INVALID` | Too late / wrong id to answer **this tap**. The card decision must already have been accepted or rejected in Paraphe; do not retry the side effect. |
| [messages.getBotCallbackAnswer](https://core.telegram.org/method/messages.getBotCallbackAnswer) | `400 BOT_RESPONSE_TIMEOUT`, `-503 Timeout` | Bot did not answer in time. User sees a client timeout. No approval happened unless Paraphe already processed a delivered update. |
| [messages.getBotCallbackAnswer](https://core.telegram.org/method/messages.getBotCallbackAnswer) | `400 MESSAGE_ID_INVALID` / `MSG_ID_INVALID` | Client tapped a message Telegram no longer treats as valid. |
| [messages.editMessage](https://core.telegram.org/method/messages.editMessage) | `400 MESSAGE_EDIT_TIME_EXPIRED`, `MESSAGE_ID_INVALID`, `MESSAGE_NOT_MODIFIED` | Visual update failed. Do not treat that as "tap is therefore invalid"; the tap path is separate. |
| [messages.editMessage](https://core.telegram.org/method/messages.editMessage) | `400 BUTTON_DATA_INVALID` | New keyboard's `callback_data` is not valid (see size limit). |

The HTTP Bot API wrapping of these errors is the generic unsuccessful response: `ok: false`, `description`, `error_code` ([Making requests](https://core.telegram.org/bots/api#making-requests)). The Bot API page does not enumerate `QUERY_ID_INVALID` on `answerCallbackQuery`. Handle `ok: false` without assuming a stable error string in HTTP.

### Visual persistence after expiry

**Not documented.** Telegram never says inline buttons vanish, dim, or stop sending callbacks when wall-clock time passes. Persistence of the **picture of the button** is a client history feature. Persistence of **authority to approve** must be Paraphe's.

## 5. `callback_data` size and uniqueness

[InlineKeyboardButton.callback_data](https://core.telegram.org/bots/api#inlinekeyboardbutton): "Data to be sent in a callback query to the bot when the button is pressed, **1-64 bytes**."

Implications:

- Count **bytes**, not Unicode characters. A UUID (36 ASCII bytes) fits. A signed blob plus card id plus action plus expiry ISO timestamp may not.
- Uniqueness across live cards must fit in 64 bytes. Bind an opaque card id + current version (or revision nonce) + action (`yes` / `no` / choice index), then look it up server-side. Do not stuff the decision payload into the button.
- MTProto `keyboardButtonCallback.data` is `bytes` with no extra published size on that constructor page ([keyboardButtonCallback](https://core.telegram.org/constructor/keyboardButtonCallback)). The Bot API 64-byte cap is the one the HTTP bot must obey. Invalid data can surface as `BUTTON_DATA_INVALID` on send/edit ([messages.editMessage](https://core.telegram.org/method/messages.editMessage)).
- `CallbackQuery.data` is optional and "exactly one of the fields `data` or `game_short_name` will be present" ([CallbackQuery](https://core.telegram.org/bots/api#callbackquery)). Paraphe buttons are `callback_data`, not games.

`answerCallbackQuery.text` is a separate budget: 0–200 characters if a toast is shown ([answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)). Use that for "this card is no longer open," not `callback_data`.

## 6. Private chat vs channel (only what is documented)

ADR 0002 is a dedicated bot in a **private** chat. Documented differences that touch this ticket:

- Chat type is `"private"`, `"group"`, `"supergroup"`, or `"channel"` ([Chat](https://core.telegram.org/bots/api#chat)).
- FAQ: all bots receive "All messages from private chats with users" ([Bots FAQ — What messages will my bot get?](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get)). Privacy mode is a **group** rule, not a private-chat rule.
- `web_app` inline buttons are "Available only in private chats between a user and the bot" ([InlineKeyboardButton.web_app](https://core.telegram.org/bots/api#inlinekeyboardbutton)). Out of scope for a yes/no tap, but it confirms private chat is a first-class Bot API surface.
- Channel-specific: `switch_inline_query_current_chat` is "Not supported in channels" ([InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)). Changelog 4.3: forwarded messages keep an inline keyboard only for URL/`login_url` (and some switch-inline) buttons, not callback buttons ([changelog, 31 May 2019](https://core.telegram.org/bots/api-changelog#may-31-2019)). A private-chat card is not forwarded into a channel in v1; if it ever were, callback buttons would not be the surviving kind.
- The 48-hour `editMessageReplyMarkup` exception for channel admins editing **other** admins' messages ([changelog, 31 May 2019](https://core.telegram.org/bots/api-changelog#may-31-2019)) does not apply to a bot editing **its own** private-chat message.
- `deleteMessage` 48-hour cap applies in private chats too ([deleteMessage](https://core.telegram.org/bots/api#deletemessage)).

**Not documented:** a shorter or longer callback-button lifetime in private chat versus channel. Do not specify one.

## 7. What the spec must require so a late tap cannot approve a dead card

Telegram will not enforce Paraphe expiry. The spec has to.

1. **Paraphe owns card liveness.** Store `open` / `cancelled` / `expired` / `tapped` on the card. A `callback_query` is a claim, not a decision. GLOSSARY.md already says the agent must revalidate live state before acting; the Telegram handler must do the same before recording a tap.

2. **`callback_data` is opaque and ≤ 64 bytes.** Bind a unique card id, current version (or revision nonce), and action. Look up the card. If missing, cancelled, expired, already tapped, or version-mismatched: do not approve. Same id must not be reused across cards.

3. **Always `answerCallbackQuery` for the received `id`.** Required by Telegram even when refusing ([CallbackQuery NOTE](https://core.telegram.org/bots/api#callbackquery); [Bot buttons](https://core.telegram.org/api/bots/buttons)). For a dead card, pass `text` (and `show_alert: true` if the owner must not miss it) saying the card is no longer open. Do not skip the answer; the client shows a progress bar until it arrives. Do not treat `QUERY_ID_INVALID` / HTTP `ok: false` on the answer as a reason to approve.

4. **Edit in place on cancel / expiry / successful tap.** Use `editMessageText` (new body: cancelled / expired / answered) plus a keyboard with no live `callback_data` (empty markup, or `DisabledButton`s). This is UX and a best-effort race reducer, not the authorization check. If edit fails (`MESSAGE_EDIT_TIME_EXPIRED` or HTTP `ok: false`), keep refusing taps in Paraphe.

5. **Do not delete as the safety mechanism.** `deleteMessage` only works for 48 hours ([deleteMessage](https://core.telegram.org/bots/api#deletemessage)). A deleted message can still yield `InaccessibleMessage` on a late query ([CallbackQuery.message](https://core.telegram.org/bots/api#callbackquery)). Still look up `data` and refuse.

6. **Do not trust delivery.** Updates older than 24 hours may never arrive ([Getting updates](https://core.telegram.org/bots/api#getting-updates)). Silence is not a tap (GLOSSARY.md). A delayed `callback_query` after cancel/expiry is refused like any other late tap.

7. **Idempotent handler.** Two taps, retries, and duplicate `getUpdates` deliveries must not double-approve. Key on card id + Paraphe version, not on "we already answered this `callback_query.id`" alone (a second tap gets a new query id).

8. **Private chat only, as ADR 0002.** Ignore `callback_query` from any `chat.type` other than `"private"`, and from any `from` other than the owner. Telegram will still deliver whatever chats the bot is in.

## 8. Explicit gaps (do not freeze as facts)

Official pages do **not** state:

- A numeric lifetime for inline callback buttons or for `callback_query.id` beyond "answer quickly" / client `BOT_RESPONSE_TIMEOUT`.
- That buttons disappear or disable themselves when a product-level card expires.
- That `editMessageText` of a bot-sent private-chat message with an inline keyboard is capped at 48 hours (the 48-hour sentence is the business-message caveat).
- The HTTP Bot API error string for a stale `answerCallbackQuery` (`QUERY_ID_INVALID` is documented on the MTProto method).

Those gaps are why the safety rule is Paraphe-side lookup, not Telegram-side TTL.

## Ticket mapping

| Ticket question | Finding |
|---|---|
| How long do inline buttons stay valid? | No documented button TTL. They stay on the message until edited/deleted. Query **delivery** is kept ≤ 24 hours. Answering **one tap** is time-bounded by client timeout / invalid query id. |
| Can a message be edited in place on cancel/expiry? | Yes. `editMessageText` / `editMessageReplyMarkup` are the documented tools for inline-keyboard messages. Prefer edit over delete (delete is 48 hours). |
| What must the spec require so a late tap cannot approve a dead card? | Opaque ≤64-byte `callback_data`; Paraphe liveness check before any approve; always `answerCallbackQuery` with a stale notice; edit keyboard as UX; ignore non-private / non-owner queries; idempotent on card id. |
