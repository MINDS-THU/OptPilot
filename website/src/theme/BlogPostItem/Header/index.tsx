import React, {type ReactNode} from 'react';
import Link from '@docusaurus/Link';
import Translate from '@docusaurus/Translate';
import {useBlogPost} from '@docusaurus/plugin-content-blog/client';
import BlogPostItemHeaderTitle from '@theme/BlogPostItem/Header/Title';
import BlogPostItemHeaderInfo from '@theme/BlogPostItem/Header/Info';
import BlogPostItemHeaderAuthors from '@theme/BlogPostItem/Header/Authors';

export default function BlogPostItemHeader(): ReactNode {
  const {isBlogPostPage} = useBlogPost();

  return (
    <header>
      {isBlogPostPage ? (
        <p className="articleReturnLink">
          <Link to="/blog"><Translate id="blog.returnToIndex">Back to all engineering notes</Translate></Link>
        </p>
      ) : null}
      <BlogPostItemHeaderTitle />
      <BlogPostItemHeaderInfo />
      <BlogPostItemHeaderAuthors />
    </header>
  );
}
