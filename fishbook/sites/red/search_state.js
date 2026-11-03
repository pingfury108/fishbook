// 从 SSR state 里取搜索结果（首屏），不做任何 API 调用
const state = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.search;
if (!state) return { error: "NO_SEARCH_STATE" };

const feeds = (__fbUnwrap(state.feeds) || []).map(__fbUnwrap);
const noteCard = (feed) => __fbUnwrap(feed.noteCard) || {};

return {
  hasMore: !!__fbUnwrap(state.hasMore),
  loginWall: document.body ? document.body.innerText.includes("登录后查看搜索结果") : false,
  feeds: feeds
    .filter((feed) => feed && feed.id)
    .map((feed) => {
      const card = noteCard(feed);
      const user = __fbUnwrap(card.user) || {};
      const interact = __fbUnwrap(card.interactInfo) || {};
      return {
        id: feed.id,
        token: __fbUnwrap(feed.xsecToken) || null,
        modelType: feed.modelType || null,
        type: card.type || null,
        title: card.displayTitle || null,
        cover: (__fbUnwrap(card.cover) || {}).urlDefault || null,
        author: user.nickname || null,
        authorId: user.userId || null,
        liked: interact.likedCount || null,
        tags: (__fbUnwrap(card.tagList) || []).map((tag) => tag && tag.name).filter(Boolean),
      };
    }),
};
