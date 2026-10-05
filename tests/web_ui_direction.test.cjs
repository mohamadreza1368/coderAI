const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web_ui/app.js"), "utf8");

// The client is a classic script that initializes the full UI on load. Evaluate
// its actual named helpers without booting the app or contacting a model server.
function helper(name) {
  const match = source.match(new RegExp(`^function ${name}\\([^]*?^\\}`, "m"));
  assert.ok(match, `Expected the production ${name} helper`);
  return match[0];
}

const isRTL = vm.runInNewContext(`${helper("isRTL")}; isRTL`);
const digitSets = ["0123456789", "۰۱۲۳۴۵۶۷۸۹", "٠١٢٣٤٥٦٧٨٩", "०१२३४५६७८९", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"];

test("decimal digits do not make English text RTL", () => {
  for (const digits of digitSets) {
    assert.equal(isRTL(`Hello ${digits}`), false, digits);
    assert.equal(isRTL(`API${digits}`), false, `attached ${digits}`);
  }
});

test("decimal-only input keeps the existing LTR fallback", () => {
  for (const digits of digitSets) {
    assert.equal(isRTL(digits), false, digits);
    assert.equal(isRTL(` (${digits})! `), false, `punctuated ${digits}`);
  }
});

test("decimal digits do not dilute the RTL ratio", () => {
  for (const digits of digitSets) {
    assert.equal(isRTL(`سلام Hello ${digits.repeat(5)}`), true, digits);
  }
});

test("the existing ratio, empty-input and punctuation policies are unchanged", () => {
  assert.equal(isRTL("Hello, world!"), false);
  assert.equal(isRTL("سلام دنیا"), true);
  assert.equal(isRTL("اabcd"), false); // Exactly 20% is still LTR.
  assert.equal(isRTL("ابabcd"), true);
  assert.equal(isRTL(""), false);
  assert.equal(isRTL(null), false);
  assert.equal(isRTL(" 123 -- () "), false);
});

test("letter-number characters are not stripped as decimal digits", () => {
  assert.equal(isRTL("اⅫⅫⅫⅫ"), false);
});

test("message rendering uses the detector without changing message text", () => {
  const messagesElement = { innerHTML: "", scrollTop: 0, scrollHeight: 0 };
  const messages = [
    { role: "user", content: "Hello ۱۲۳۴۵۶" },
    { role: "assistant", content: "Hello ١٢٣٤٥٦" },
    { role: "assistant", content: "سلام Hello १२३४५६७८९" },
  ];
  const originals = messages.map((message) => message.content);
  const context = {
    state: { data: { messages } },
    $: (id) => {
      assert.equal(id, "messages");
      return messagesElement;
    },
  };
  vm.runInNewContext(
    ["isRTL", "escapeHtml", "extractCodeBlocks", "renderMessageContent", "renderMessages"]
      .map(helper).join("\n") + "\nrenderMessages();",
    context,
  );
  const directions = Array.from(messagesElement.innerHTML.matchAll(/<article[^>]+dir="(rtl|ltr)"/g), (match) => match[1]);
  assert.deepEqual(directions, ["ltr", "ltr", "rtl"]);
  for (const original of originals) assert.ok(messagesElement.innerHTML.includes(original));
  assert.deepEqual(messages.map((message) => message.content), originals);
});

test("mixed Persian and English reports with code blocks are rendered RTL", () => {
  const mixedReport = `# Project Report
تحلیل کامل پروژه در ادامه آورده شده است:
- models/user.py: Handles User model, password hashing with bcrypt, email validation.
- controllers/auth_controller.py: Handles login, registration, token verification.
\`\`\`python
class UserManager:
    def __init__(self, db_session):
        pass
\`\`\``;
  assert.equal(isRTL(mixedReport), true);
});

test("pure English technical reports with code blocks are rendered LTR", () => {
  const englishReport = `# Project Overview
The architecture is divided into modules:
- auth.py: Handles login
- database.py: SQLite connection
\`\`\`python
def connect(): pass
\`\`\``;
  assert.equal(isRTL(englishReport), false);
});
