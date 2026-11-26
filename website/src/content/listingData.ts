export type CaseStudyTone = 'amber' | 'slate' | 'teal';

export type UseCasePreview = {
  title: string;
  href: string;
  tags: string[];
  metric: string;
  previewImage: string;
  imageTitle: string;
  imageCaption: string;
  abstract: string;
  tone: CaseStudyTone;
  featured: boolean;
  position: number;
};

export type BlogPreview = {
  title: string;
  href: string;
  description: string;
  date: string;
  previewImage: string;
};

type RequireContext = {
  keys(): string[];
  <T>(id: string): T;
};

declare const require: {
  context(path: string, deep?: boolean, filter?: RegExp): RequireContext;
};

type GeneratedDoc = {
  title: string;
  description?: string;
  permalink: string;
  sidebarPosition?: number;
  frontMatter: {
    featured?: boolean;
    preview_image?: string;
    preview_metric?: string;
    preview_tags?: string[];
    preview_visual_title?: string;
    preview_visual_description?: string;
    preview_summary?: string;
    preview_tone?: CaseStudyTone;
  };
};

type GeneratedBlog = {
  title: string;
  permalink: string;
  description?: string;
  date: string;
  frontMatter?: {
    preview_image?: string;
  };
};

const toSitePath = (permalink: string): string =>
  permalink.replace(/^\/OptPilot(?:\/zh-CN)?/, '') || '/';

const docsContext = require.context(
  '@generated/docusaurus-plugin-content-docs/default',
  false,
  /site-content-docs-case-studies-.*\.json$/,
);

export const useCasePreviews: UseCasePreview[] = docsContext
  .keys()
  .map((key) => docsContext<GeneratedDoc>(key))
  .map((doc) => ({
    title: doc.title,
    href: toSitePath(doc.permalink),
    tags: doc.frontMatter.preview_tags ?? [],
    metric: doc.frontMatter.preview_metric ?? '',
    previewImage: doc.frontMatter.preview_image ?? '',
    imageTitle: doc.frontMatter.preview_visual_title ?? doc.title,
    imageCaption: doc.frontMatter.preview_visual_description ?? doc.description ?? '',
    abstract: doc.frontMatter.preview_summary ?? doc.description ?? '',
    tone: doc.frontMatter.preview_tone ?? 'teal',
    featured: doc.frontMatter.featured ?? false,
    position: doc.sidebarPosition ?? 0,
  }))
  .sort((left, right) => left.position - right.position);

export const featuredUseCasePreviews = useCasePreviews.filter(
  (useCase) => useCase.featured,
);

const blogContext = require.context(
  '@generated/docusaurus-plugin-content-blog/default',
  false,
  /site-content-blog-.*\.json$/,
);

export const blogPreviews: BlogPreview[] = blogContext
  .keys()
  .map((key) => blogContext<GeneratedBlog>(key))
  .map((post) => ({
    title: post.title,
    href: toSitePath(post.permalink),
    description: post.description ?? '',
    date: post.date,
    previewImage: post.frontMatter?.preview_image ?? '',
  }))
  .sort((left, right) => right.date.localeCompare(left.date));
