import {useLatestVersion} from '@docusaurus/plugin-content-docs/client';

type ResearchArticle = {
  title: string;
  permalink: string;
  sidebarPosition: number;
  frontMatter: {featured?: boolean; preview_summary: string};
};

declare const require: {
  context(path: string, deep: boolean, filter: RegExp): {
    keys(): string[];
    (key: string): ResearchArticle;
  };
};

const articles = require.context(
  '@generated/docusaurus-plugin-content-docs/default',
  false,
  /case-studies-.*\.json$/,
);

export function useFeaturedResearchArticles(): ResearchArticle[] {
  const version = useLatestVersion(undefined);
  const paths = new Set(version.docs.map(doc => doc.path));
  return articles.keys()
    .map(key => articles(key))
    .filter(article => paths.has(article.permalink) && article.frontMatter.featured)
    .sort((left, right) => left.sidebarPosition - right.sidebarPosition);
}
