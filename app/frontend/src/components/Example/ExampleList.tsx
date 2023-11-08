import { Example } from "./Example";

import styles from "./Example.module.css";

export type ExampleModel = {
    text: string;
    value: string;
};

const EXAMPLES: ExampleModel[] = [
    {
        text: "How does data classification affect user access control?",
        value: "How does data classification affect user access control?"
    },
    { text: "What are the responsibilities of a Data Steward?", value: "What are the responsibilities of a Data Steward?" },
    { text: "What steps are involved in a data quality audit?", value: "What steps are involved in a data quality audit?" }
];

interface Props {
    onExampleClicked: (value: string) => void;
}

export const ExampleList = ({ onExampleClicked }: Props) => {
    return (
        <ul className={styles.examplesNavList}>
            {EXAMPLES.map((x, i) => (
                <li key={i}>
                    <Example text={x.text} value={x.value} onClick={onExampleClicked} />
                </li>
            ))}
        </ul>
    );
};
