// -*- mode: js -*-
/** 飞书发送器：把文本发到群（chat_id）。live 模式用官方 SDK，测试模式可注入 fake。 */
import * as Lark from '@larksuiteoapi/node-sdk';
import { config } from './config.mjs';

export function createFeishuSender(cfg = config, { client } = {}) {
  const feishu =
    client ||
    new Lark.Client({
      appId: cfg.appId,
      appSecret: cfg.appSecret,
      appType: Lark.AppType.SelfBuild,
      domain: Lark.Domain.Feishu,
    });

  async function sendText(chatId, text) {
    const resp = await feishu.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: 'text',
        content: JSON.stringify({ text }),
      },
    });
    if (resp?.code && resp.code !== 0) {
      throw new Error(`飞书发送失败 code=${resp.code} msg=${resp.msg || ''}`);
    }
    return resp;
  }

  return { sendText };
}

export function createFakeSender() {
  const sent = [];
  return {
    sent,
    async sendText(chatId, text) {
      sent.push({ chatId, text });
      return { code: 0, data: { message_id: `om_mock_${sent.length}` } };
    },
  };
}
