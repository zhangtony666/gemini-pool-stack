import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { generateText, streamText } from 'ai';

const provider = createOpenAICompatible({
  name: 'image-verifier',
  baseURL: process.env.IV_TEST_BASE_URL,
  apiKey: 'smoke-business-key',
});
const model = provider.chatModel('image-verifier-vision');
const connected = await generateText({model, prompt: '/help', maxRetries: 0});
assert.match(connected.text, /已连接/);
const content = await readFile(process.env.IV_TEST_IMAGE);
const output = streamText({
  model, maxRetries: 0,
  messages: [{role: 'user', content: [
    {type: 'text', text: '请核验图片'},
    {type: 'image', image: content, mediaType: 'image/png'},
  ]}],
});
let text = '';
for await (const piece of output.textStream) text += piece;
assert.match(text, /模拟结果/);
assert.equal(await output.finishReason, 'stop');
console.log(JSON.stringify({cherryRelease: 'v2.1.2', sdkContract: true, streamedImage: true}));
