({
  // Please visit the URL below for more information:
  // https://shd101wyy.github.io/markdown-preview-enhanced/#/extend-parser

  onWillParseMarkdown: async function(markdown) {
    // Markdown 生テキストを前処理したい場合のフック。
    return markdown;
  },

  onDidParseMarkdown: async function(html) {
    // :::name ... ::: を <div class="name"> に変換する。
    // ラベル(絵文字)と配色は style.less の .callout / ::before 側で付与する。
    const callouts = ['note', 'tip', 'info', 'source', 'warning', 'caution', 'sample', 'result'];

    let html_ = html;
    for (const name of callouts) {
      // Markdown が付けた前後の <p> ごと差し替え、div の入れ子崩れを防ぐ。
      const re = new RegExp(`(?:<p>\\s*)?:::${name}([\\w\\W]+?):::(?:\\s*</p>)?`, 'gi');
      html_ = html_.replace(re, (whole, content) => `<div class="${name}">${content.trim()}</div>`);
    }
    return html_;
  },
})
