import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import Translate from '@docusaurus/Translate';
import {projectLinks} from '@site/src/data/homeContent';
import styles from './styles.module.css';

export default function OverviewPage(): ReactNode {
  return (
    <div className={styles.page}>
      <header className={styles.intro}>
        <h1><Translate id="overview.title">Designing a comparison in OptPilot</Translate></h1>
        <p className={styles.lede}><Translate id="overview.lead">A scheduling policy can finish a short factory shift with a higher score simply because fewer orders arrived. To find out what the policy changed, evaluate both versions on the same shift and read the measurements behind their scores.</Translate></p>
      </header>
      <section className={styles.section}>
        <div className={styles.sectionHeading}><h2><Translate id="overview.design.title">Choose what varies and what stays fixed</Translate></h2></div>
        <p><Translate id="overview.design.text">The factory simulator models the production line. A task defines what the policy can observe, which commands it can issue and how the grader scores a shift. An instance specifies the conditions for one shift. A suite lets every policy face the same set of shifts.</Translate></p>
        <p><Translate id="overview.review.text">Collect the policies and cases in a study. Review saves their exact project versions along with the metric, repetitions and execution settings. You can still inspect that comparison after changing the simulator or revising a policy.</Translate></p>
      </section>
      <section className={styles.section}>
        <div className={styles.sectionHeading}><h2><Translate id="overview.compare.title">Decide what to try next</Translate></h2></div>
        <p><Translate id="overview.compare.text">Open a trial to see which policy ran, how it scored and what evidence the grader returned. A failed attempt carries its error. When you read averages, check how many cases yielded measurements.</Translate></p>
        <p><Translate id="overview.generate.text">If a researcher can propose the next policy, save it as a new candidate. If a program can propose it, register that program as a generator and specify the feedback it may receive and the number of proposals to evaluate.</Translate></p>
      </section>
      <nav className={styles.nextSteps} aria-label="OptPilot guides">
        <Link href={projectLinks.gettingStarted}><strong><Translate id="overview.next.start">Run a local comparison</Translate></strong><span><Translate id="overview.next.startText">Compare two search algorithms on five questions and explain one difference in their rankings.</Translate></span><b>→</b></Link>
        <Link href={projectLinks.resources}><strong><Translate id="overview.next.resource">Run with a shared instrument</Translate></strong><span><Translate id="overview.next.resourceText">Register a simulated colorimeter, inspect its allocation and recover a waiting run.</Translate></span><b>→</b></Link>
        <Link to={projectLinks.desktop}><strong>OptPilot Desktop</strong><span><Translate id="overview.next.desktopText">Inspect results in the project graph and take a trial into a conversation.</Translate></span><b>→</b></Link>
      </nav>
    </div>
  );
}
