package org.dk.dtu.man;

import com.healthmarketscience.jackcess.*;

import org.tinylog.Logger;
import java.io.*;
import java.util.List;
import java.util.stream.Collectors;

// Simple script to write a database from TU data (in .accdb format) to a csv file.
// So far only tested with "journey", "session", and "tur" tables
// Fields containing commas, quotes or line breaks are quoted
public class WriteToCSV {

    public static final String SEP = ",";

    public static void main(String[] args) throws IOException {

        if(args.length != 3) {
            throw new RuntimeException("""
                    Program requires exactly 3 arguments:
                    (0) Path to access database (ending in .accdb)
                    (1) Output csv file path (ending in .csv)
                    (2) Name of database table to convert
                    """);
        }

        Database db = new DatabaseBuilder(new File(args[0])).setReadOnly(true).open();
        Table table = db.getTable(args[2]);
        if(table == null) {
            throw new RuntimeException("Database table '" + args[2] + "' not found!\n" +
                    "Tables in database: " + db.getTableNames().toString());
        }


        // Get column names
        List<String> colNames = table.getColumns().stream().map(Column::getName).toList();
        Logger.info("opened table " + args[2] + " which has " + colNames.size() + " columns");

        // Write column types for R based on the read_delim short form (Useful for reading into R using readr::read_delim)
        String s = table.getColumns().stream().map(Column::getType).map(WriteToCSV::getRType).collect(Collectors.joining(""));
        Logger.info("col_types argument for R: c" + s);

        // Open new file
        Logger.info("opening file '" + args[1] + "'");
        System.out.println();
        PrintWriter out = openFileForSequentialWriting(new File(args[1]),false);
        assert out != null;

        // Print header
        Logger.info("writing header...");
        StringBuilder builder = new StringBuilder();
        builder.append("rowId").append(SEP).append(String.join(SEP, colNames));
        out.println(builder);

        // Print each row
        int rowCount = table.getRowCount();
        Logger.info("Writing " + rowCount + " rows");
        for(int i = 0 ; i < rowCount ; i++) {
            builder = new StringBuilder();
            Row row = table.getNextRow();
            builder.append(row.getId().toString());
            for(String colName : colNames) {
                builder.append(SEP);
                Object value = row.get(colName);
                if(value != null) {
                    builder.append(toCsvField(value.toString()));
                }
            }
            out.println(builder);

            // Status indicator
            if(i == 0) {
                System.out.print("0%");
            } else if(i == (int) (rowCount * 0.25)) {
                System.out.print("25%");
            } else if(i == (int) (rowCount * 0.5)) {
                System.out.print("50%");
            } else if(i == (int) (rowCount * 0.75)) {
                System.out.print("75%");
            } else if(i == rowCount - 1) {
                System.out.println("100%");
            } else if(i % (rowCount / 20) == 0)  {
                System.out.print(".");
            }
        }

        // Close text file after writing
        out.close();
        Logger.info("Closed csv file");

        // Close database
        db.close();
        Logger.info("Closed database file. Finished.");

    }

    // Gets the corresponding R column type (see read_delim help in R Documentation)
    public static String getRType(DataType type) {
        return switch (type) {
            case BYTE, INT, LONG -> "i";
            case FLOAT, DOUBLE, NUMERIC, MONEY -> "n";
            case BOOLEAN -> "l";
            case TEXT, MEMO, GUID -> "c";
            case SHORT_DATE_TIME, EXT_DATE_TIME -> "T";
            default -> "?"; // let readr guess
        };
    }

    // Quotes a field containing the separator, quotes or line breaks (RFC 4180)
    public static String toCsvField(String s) {
        if(s.contains(SEP) || s.contains("\"") || s.contains("\n") || s.contains("\r")) {
            return "\"" + s.replace("\"", "\"\"") + "\"";
        }
        return s;
    }

    // Opens or appends file for writing
    public static PrintWriter openFileForSequentialWriting(File outputFile, boolean append) {
        if (outputFile.getParent() != null) {
            File parent = outputFile.getParentFile();
            parent.mkdirs();
        }

        try {
            FileWriter fw = new FileWriter(outputFile, append);
            BufferedWriter bw = new BufferedWriter(fw);
            return new PrintWriter(bw);
        } catch (IOException var5) {
            System.out.println("Could not open file <" + outputFile.getName() + ">.");
            return null;
        }
    }
}