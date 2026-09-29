// A machine-readable signal for "which build am I actually looking at" -
// added because it was hard to tell, during testing, whether the live site
// reflected a just-merged change or a stale build/cache (see the async
// build-trigger + warm-Lambda-cache staleness traps in
// gds_idea_cdk_constructs' StaticSite construct - there's a real gap
// between `cdk deploy` succeeding and the new content actually being live).
//
// `new Date()` is evaluated once, synchronously, exactly when this file is
// processed - which is every time `npx @11ty/eleventy` actually runs inside
// BuildLambda (see site_src/handler.py). A stale/old builtAt here is
// unambiguous proof you're not looking at a fresh build.
export const data = {
  permalink: '/build-info.json',
  eleventyExcludeFromCollections: true,
};

export function render() {
  return JSON.stringify({ builtAt: new Date().toISOString() });
}
