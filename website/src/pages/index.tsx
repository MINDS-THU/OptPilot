import type {ReactNode} from 'react';
import Heading from '@theme/Heading';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Translate, {translate} from '@docusaurus/Translate';

import {useFeaturedResearchArticles} from '@site/src/content/listingData';
import {projectLinks} from '@site/src/data/homeContent';
import styles from './index.module.css';

function SearchComparison(): ReactNode {
  return (
    <figure className={styles.resultCard}>
      <figcaption>
        <strong><Translate id="homepage.result.title">Which ranker finds the right help article?</Translate></strong>
        <span><Translate id="homepage.result.setup">Five labeled questions · six articles · one trial per ranker and question</Translate></span>
      </figcaption>
      <table>
        <thead>
          <tr>
            <th scope="col"><Translate id="homepage.result.ranker">Ranker</Translate></th>
            <th scope="col"><Translate id="homepage.result.first">Correct at rank 1</Translate></th>
            <th scope="col"><Translate id="homepage.result.mrr">Mean reciprocal rank</Translate></th>
          </tr>
        </thead>
        <tbody>
          <tr><th scope="row"><Translate id="homepage.result.wordCount">Word count</Translate></th><td>3 / 5</td><td>0.767</td></tr>
          <tr><th scope="row">BM25</th><td>5 / 5</td><td>1.000</td></tr>
        </tbody>
      </table>
      <p><Translate id="homepage.result.example">On the question about editing a copy, word count ranked the intended article third. BM25 ranked it first. Open the two trials to compare their ranked lists.</Translate></p>
      <Link href={projectLinks.gettingStarted}><Translate id="homepage.result.link">Reproduce this comparison</Translate> →</Link>
    </figure>
  );
}

function HomepageHeader(): ReactNode {
  return (
    <header className={styles.hero}>
      <div className={`${styles.container} ${styles.heroLayout}`}>
        <div className={styles.heroCopy}>
          <Heading as="h1" className={styles.heroTitle}>
            <Translate id="homepage.hero.title">Did the new solution improve?</Translate>
          </Heading>
          <p className={styles.heroLead}>
            <Translate id="homepage.hero.lead">OptPilot is an open-source framework for iterative optimization. Compare policies, programs or parameter settings on the same cases, inspect where their behavior differs, and use those results to design the next candidate.</Translate>
          </p>
          <p className={styles.heroDetail}>
            <Translate id="homepage.hero.subtitle">Each comparison preserves the selected versions of the candidates, cases and grader. You can change the project and still trace an earlier score to the trial that produced it.</Translate>
          </p>
          <div className={styles.heroActions}>
            <Link className="button button--primary" href={projectLinks.gettingStarted}><Translate id="homepage.hero.getStarted">Run your first experiment</Translate> →</Link>
            <Link className={styles.inlineLink} href={projectLinks.code}>GitHub ↗</Link>
          </div>
        </div>
        <SearchComparison />
      </div>
    </header>
  );
}

function WorkflowSection(): ReactNode {
  return (
    <section className={styles.section} id="platform">
      <div className={`${styles.container} ${styles.workflowLayout}`}>
        <div>
          <Heading as="h2"><Translate id="homepage.workflow.title">From a baseline to the next candidate</Translate></Heading>
          <ol className={styles.workflowList}>
            <li><span>1</span><div>
              <Heading as="h3"><Translate id="homepage.workflow.define.title">Define a comparison</Translate></Heading>
              <p><Translate id="homepage.workflow.define.description">Choose the cases, what each candidate may change and the metric that matters. Review fixes the selected project versions and run settings.</Translate></p>
            </div></li>
            <li><span>2</span><div>
              <Heading as="h3"><Translate id="homepage.workflow.run.title">Inspect individual trials</Translate></Heading>
              <p><Translate id="homepage.workflow.run.description">Open a candidate’s result for one case. Read its measurements, the grader’s evidence and any errors alongside the average.</Translate></p>
            </div></li>
            <li><span>3</span><div>
              <Heading as="h3"><Translate id="homepage.workflow.decide.title">Try a revision</Translate></Heading>
              <p><Translate id="homepage.workflow.decide.description">Submit your own change or let a generator propose candidates within a budget. The task defines which scores the generator can use as feedback.</Translate></p>
            </div></li>
          </ol>
          <Link className={styles.inlineLink} href={projectLinks.generators}><Translate id="homepage.workflow.link">Run a candidate search</Translate> →</Link>
        </div>
        <aside className={styles.quickStart}>
          <div className={styles.quickStartHeader}><strong><Translate id="homepage.calibration.title">A comparison with one shared instrument</Translate></strong></div>
          <div className={styles.benchText}>
            <p><Translate id="homepage.calibration.problem">Two display settings need measurements at two gray levels. The simulated colorimeter has two channels, but the team can reserve only one at a time.</Translate></p>
            <p><Translate id="homepage.calibration.result">When another user holds the team’s seat, the run waits. It resumes under the same ID and records the reservation, RGB readings and release for each trial.</Translate></p>
            <Link href={projectLinks.resources}><Translate id="homepage.calibration.link">Run the resource allocation tutorial</Translate> →</Link>
          </div>
        </aside>
      </div>
    </section>
  );
}

function DesktopSection(): ReactNode {
  return (
    <section className={`${styles.section} ${styles.studioSection}`} id="desktop">
      <div className={styles.container}>
        <div className={styles.studioHeader}>
          <Heading as="h2"><Translate id="homepage.studio.title">Investigate a result in Desktop</Translate></Heading>
          <p><Translate id="homepage.studio.description">Follow a score back to the candidate and case in the project graph. Attach a trial to an assistant conversation, revise the program, and compare the new version on the same suite. The CLI reads the same project and results.</Translate></p>
        </div>
        <div className={styles.sectionActions}>
          <Link className="button button--primary" to={projectLinks.desktop}><Translate id="homepage.studio.cta">Explore Desktop</Translate> →</Link>
          <a className="button button--secondary" href={projectLinks.releases}><Translate id="homepage.studio.releases">Desktop releases</Translate> ↗</a>
        </div>
      </div>
    </section>
  );
}

function ResearchPackagesSection(): ReactNode {
  const articles = useFeaturedResearchArticles();
  return (
    <section className={`${styles.section} ${styles.researchSection}`} id="research">
      <div className={styles.container}>
        <div className={styles.researchHeader}>
          <div className={styles.sectionHeader}><Heading as="h2"><Translate id="homepage.research.title">Experiment with a simulator or solver</Translate></Heading></div>
          <p><Translate id="homepage.research.description">Each package starts with a different research question. Its guide explains the inputs, the evaluation method and what you can conclude from a run.</Translate></p>
        </div>
        <div className={styles.packageLinks}>
          {articles.map(item => (
            <article key={item.permalink}>
              <Heading as="h3"><Link to={item.permalink}>{item.title}</Link></Heading>
              <p>{item.frontMatter.preview_summary}</p>
            </article>
          ))}
        </div>
        <Link className={styles.inlineLink} href={projectLinks.catalog}><Translate id="homepage.research.viewAll">Browse all six example projects</Translate> →</Link>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title={translate({id: 'homepage.meta.title', message: 'Experiments for iterative optimization'})}
      description={translate({id: 'homepage.meta.description', message: 'Compare candidate solutions on shared cases and use trial evidence to guide the next iteration.'})}>
      <HomepageHeader />
      <main><WorkflowSection /><DesktopSection /><ResearchPackagesSection /></main>
    </Layout>
  );
}
