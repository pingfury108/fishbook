// 从 SSR state 里取单篇笔记详情，不做任何 API 调用
const state = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.note;
if (!state) return { error: "NO_NOTE_STATE" };

const detailMap = __fbUnwrap(state.noteDetailMap) || {};
const currentId = __fbUnwrap(state.currentNoteId) || __fbUnwrap(state.firstNoteId);
const entry = detailMap[currentId] || Object.values(detailMap)[0];
const note = entry ? __fbUnwrap(entry.note) : null;
if (!note) return { error: "NO_NOTE_CONTENT" };

const stream = ((note.video || {}).media || {}).stream || {};
const videoUrl = ["h264", "h265", "av1"]
  .map((codec) => ((stream[codec] || [])[0] || {}).masterUrl)
  .find(Boolean) || null;

return {
  note: {
    noteId: note.noteId || currentId || null,
    title: note.title || null,
    desc: note.desc || null,
    type: note.type || null,
    time: note.time || null,
    ipLocation: note.ipLocation || null,
    author: note.user ? { id: note.user.userId || null, nick: note.user.nickname || null } : null,
    interact: __fbUnwrap(note.interactInfo) || {},
    images: (__fbUnwrap(note.imageList) || [])
      .map((image) => (image && (image.urlDefault || image.urlPre)) || null)
      .filter(Boolean),
    tags: (__fbUnwrap(note.tagList) || []).map((tag) => tag && tag.name).filter(Boolean),
    video: videoUrl,
  },
};
